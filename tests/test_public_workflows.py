import json
import subprocess
from pathlib import Path

from chatgpt_exporter import main

ROOT = Path(__file__).resolve().parents[1]


def test_documented_synthetic_demo_imports_indexes_and_backs_up_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "invalid\ncredential")
    output = tmp_path / "demo"
    source = ROOT / "examples/conversations.json"
    assert (
        main(["import", str(source), "--output", str(output), "--no-attachments", "--no-progress"])
        == 0
    )
    assert (output / "index.html").is_file()
    assert json.loads((output / ".export-index.json").read_text())
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "invalid\ncredential")
    assert main(["index", str(output), "--no-progress"]) == 0
    assert main(["zip", str(output), "--no-progress"]) == 0
    assert main(["unzip", str(output.with_name("demo.zip")), "--no-progress"]) == 0
    assert (tmp_path / "demo_restored/index.html").is_file()


def test_generated_data_and_backup_paths_are_ignored_but_example_is_trackable():
    private_paths = [
        "mise.local.toml",
        ".env",
        "chatgpt_export_example/chat.json",
        "exports/custom/chat.json",
        "backup.zip",
        "custom_restored/chat.json",
        "conversations.json",
        "probe-progress.log",
        ".local/publication-audit/report.json",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input="\n".join(private_paths) + "\n",
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    assert set(result.stdout.splitlines()) == set(private_paths)
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "examples/conversations.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
