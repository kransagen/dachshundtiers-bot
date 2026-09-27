"""D6 — legacy writer audit tests (services.phase_d.legacy_writers)."""

from __future__ import annotations

from pathlib import Path

from services.phase_d.legacy_writers import (
    build_writers_report,
    classify_site,
    scan_writers,
)


def _tree(root: Path) -> None:
    (root / "cogs").mkdir(parents=True)
    (root / "db").mkdir()
    files = {
        "cogs/ht3.py": "def a():\n    save_data(x)\n",
        "services/websync.py": "push_players(payload)\n",
        "services/store.py": "x = 1\ndef save_data(p):\n    write_text(p)\n",
        "db/core.py": "import storage\nx = save_data(1)\n",
        "tests/test_x.py": "save_data(fake)\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_scan_and_classify(tmp_path):
    _tree(tmp_path)
    sites = scan_writers(tmp_path)
    modules = {(s["module"], s["line"]) for s in sites}

    assert ("cogs/ht3.py", 2) in modules
    assert ("services/websync.py", 1) in modules
    assert ("services/store.py", 2) in modules
    assert all(s["context"] for s in sites)

    assert classify_site("cogs/ht3.py", 2) == "legacy_operational_record"
    assert classify_site("cogs/edituser.py", 1136) == "identity_claim_dualwrite"
    assert classify_site("services/websync.py", 1) == "export_only"
    assert classify_site("services/store.py", 2) == "storage_layer_atomic_writer"
    assert classify_site("unknown/mod.py", 1) == "unclassified"


def test_report_classifies_and_catches_db_write_import(tmp_path):
    _tree(tmp_path)
    report = build_writers_report(tmp_path)

    by_class = {s["module"]: s["classification"] for s in report["sites"]}
    assert by_class["cogs/ht3.py"] == "legacy_operational_record"
    assert by_class["services/websync.py"] == "export_only"
    assert by_class["services/store.py"] == "storage_layer_atomic_writer"
    assert report["writers_found"] == 4  # 3 runtime + violation in db/core.py; tests/ excluded
    assert "tests" not in " ".join(s["module"] for s in report["sites"])

    audit = report["direction_audit"]
    assert audit["pg_imports_storage_with_write_api"] == ["db/core.py"]
    assert audit["pg_readonly_backend_probes"] == []
    assert audit["pg_to_json_to_discord_path"] is True
    assert report["conclusion"]


def test_clean_tree_and_readonly_probe_are_not_violations(tmp_path):
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "readonly.py").write_text(
        "import storage\np = storage.DATA_DIR\n"
    )
    (tmp_path / "cogs").mkdir(parents=True)
    (tmp_path / "cogs" / "x.py").write_text("save_data(1)\n")

    report = build_writers_report(tmp_path)

    audit = report["direction_audit"]
    assert audit["pg_imports_storage_with_write_api"] == []
    assert audit["pg_readonly_backend_probes"] == ["db/readonly.py"]
    assert audit["pg_to_json_to_discord_path"] is False
    assert report["writers_found"] == 1