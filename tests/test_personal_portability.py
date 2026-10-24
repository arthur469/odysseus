"""Recovered optional adapters resolve the current user's home, not one PC."""
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize('module,directory', [
    ('journal_ia_action', 'Journal-IA'), ('pc_automations', 'Automatisations-PC'),
])
def test_external_adapter_uses_current_user_home(tmp_path, monkeypatch, module, directory):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    source = Path(__file__).resolve().parents[1] / 'src' / (module + '.py')
    spec = importlib.util.spec_from_file_location('isolated_' + module, source)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    assert loaded.ROOT == tmp_path / 'Documents' / directory
