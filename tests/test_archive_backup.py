import os
import stat
import zipfile

import pytest

from archive_backup import pack_directory, unpack_directory
from chatgpt_client import ExportError
from chatgpt_exporter import main


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_roundtrip_preserves_originals_hidden_assets_empty_folders_and_private_modes(tmp_path):
    source = tmp_path / "export"
    (source / ".chatgpt-assets/files").mkdir(parents=True)
    (source / ".chatgpt-index/chats").mkdir(parents=True)
    (source / "empty").mkdir()
    for relative, data in {
        ".export-index.json": b"{}",
        ".chatgpt-assets/files/data.bin": bytes(range(256)),
        ".chatgpt-index/chats/chat.html": b"<p>synthetic</p>",
        "chat.json": b'{"mapping":{}}',
    }.items():
        (source / relative).write_bytes(data)
    before = snapshot(source)
    archive = tmp_path / "backup.zip"
    pack_directory(source, archive)
    saved_zip = archive.read_bytes()
    target = tmp_path / "restored"
    unpack_directory(archive, target)
    assert snapshot(source) == snapshot(target) == before
    assert archive.read_bytes() == saved_zip
    assert (target / "empty").is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        assert all(
            stat.S_IMODE(p.stat().st_mode) == 0o600 for p in target.rglob("*") if p.is_file()
        )


def test_pack_never_overwrites_without_explicit_flag(tmp_path):
    source = tmp_path / "export"
    source.mkdir()
    (source / "one").write_text("new")
    target = tmp_path / "backup.zip"
    target.write_text("previous backup")
    with pytest.raises(ExportError):
        pack_directory(source, target)
    assert target.read_text() == "previous backup"
    pack_directory(source, target, overwrite=True)
    assert zipfile.is_zipfile(target) and (source / "one").read_text() == "new"


@pytest.mark.parametrize(
    "name", [".env", ".env.production", "mise.local.toml", "mise.test.local.toml", ".git"]
)
def test_pack_refuses_accidental_credentials_and_git_checkout(tmp_path, name):
    source = tmp_path / "export"
    source.mkdir()
    (source / name).write_text("PRIVATE_TEST_VALUE")
    with pytest.raises(ExportError, match="credentials|Git checkout"):
        pack_directory(source, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()


def test_pack_refuses_nested_destination_and_symlink_sources(tmp_path):
    source = tmp_path / "export"
    source.mkdir()
    with pytest.raises(ExportError):
        pack_directory(source, source / "backup.zip")
    (source / "link").symlink_to(tmp_path)
    with pytest.raises(ExportError):
        pack_directory(source, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:/drive",
        "folder/../../escape",
        "folder\\escape",
        "a//b",
        "./file",
        "name:stream",
        "CON.txt",
        "folder/trailing.",
    ],
)
def test_unsafe_paths_are_rejected_before_any_restore(tmp_path, name):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("safe", "safe")
        z.writestr(name, "bad")
    with pytest.raises(ExportError):
        unpack_directory(archive, tmp_path / "restored")
    assert archive.exists() and not (tmp_path / "restored").exists()
    assert not (tmp_path / "escape").exists()


def test_symlink_and_device_entries_are_rejected(tmp_path):
    for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
        archive = tmp_path / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as z:
            info = zipfile.ZipInfo("special")
            info.external_attr = (mode | 0o777) << 16
            z.writestr(info, "target")
        with pytest.raises(ExportError):
            unpack_directory(archive, tmp_path / "restored")
        assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "names", [("a", "A"), ("a", "a/child"), ("a", "a/"), ("caf\u00e9", "cafe\u0301")]
)
def test_collisions_and_file_directory_conflicts_are_rejected(tmp_path, names):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name in names:
            z.writestr(name, "data")
    with pytest.raises(ExportError):
        unpack_directory(archive, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_restore_never_merges_and_size_limit_does_not_leave_partial_output(tmp_path):
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("one", b"123456")
    target = tmp_path / "restored"
    with pytest.raises(ExportError):
        unpack_directory(archive, target, max_bytes=5)
    assert not target.exists()
    target.mkdir()
    (target / "previous").write_text("keep")
    with pytest.raises(ExportError):
        unpack_directory(archive, target)
    assert snapshot(target) == {"previous": b"keep"}


def test_interrupted_pack_preserves_existing_backup_and_cleans_temp(tmp_path, monkeypatch):
    source = tmp_path / "export"
    source.mkdir()
    (source / "data").write_text("data")
    target = tmp_path / "backup.zip"
    target.write_text("previous")

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(zipfile.ZipFile, "open", interrupt)
    with pytest.raises(KeyboardInterrupt):
        pack_directory(source, target, overwrite=True)
    assert target.read_text() == "previous" and snapshot(source) == {"data": b"data"}
    assert not list(tmp_path.glob(".backup-*"))


def test_corrupt_restore_retains_zip_and_removes_staging(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("data", b"unique-payload")
    raw = archive.read_bytes().replace(b"unique-payload", b"broken-payload")
    archive.write_bytes(raw)
    with pytest.raises(zipfile.BadZipFile):
        unpack_directory(archive, tmp_path / "restored")
    assert archive.read_bytes() == raw
    assert not (tmp_path / "restored").exists() and not list(tmp_path.glob(".restore-*"))


def test_default_cli_targets_keep_directory_zip_and_existing_output(tmp_path, monkeypatch):
    root = tmp_path / "export"
    root.mkdir()
    (root / "chat.json").write_text("{}")
    monkeypatch.setenv("CHATGPT_OUTPUT_DIR", str(root))
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "invalid\ncredential")
    assert main(["zip", "--no-progress"]) == 0
    assert (tmp_path / "export.zip").is_file() and root.is_dir()
    assert main(["unzip", "--no-progress"]) == 0
    assert snapshot(tmp_path / "export_restored") == snapshot(root)
    assert main(["unzip", "--no-progress"]) == 1
    assert (tmp_path / "export.zip").is_file()


def test_explicit_cli_paths_and_overwrite_validation(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    (root / "data").write_text("data")
    archive = tmp_path / "custom.zip"
    assert main(["zip", str(root), "--output", str(archive), "--no-progress"]) == 0
    assert main(["unzip", str(archive), "--output", str(tmp_path / "custom"), "--no-progress"]) == 0
    with pytest.raises(SystemExit):
        main(["unzip", str(archive), "--overwrite"])
    with pytest.raises(SystemExit):
        main(["unzip", str(archive), "--max-unpacked-gib", "0"])
