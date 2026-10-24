"""Journal offsets refer to physical bytes, including on Windows CRT descriptors."""
from src.agent_runtime.effect_log import EffectLog
from src.agent_runtime.effects import OperationRef, ExecutionOutcome, Impact


def test_multiple_claims_and_outcomes_keep_exact_byte_offsets(tmp_path):
    log = EffectLog('b' * 32, directory=tmp_path)
    for index, tool in enumerate(('get_workspace', 'ls', 'bash')):
        effect_id = 'effect-' + str(index)
        log.claim(effect_id=effect_id, action_id='action-' + str(index),
                  operation=OperationRef(tool, '', '0' * 64))
        log.outcome(effect_id=effect_id, execution=ExecutionOutcome.REPORTED_SUCCESS,
                    impact=Impact.POSSIBLE, execution_id='execution-' + str(index))
        assert log._offset == log.path.stat().st_size
        assert not log.degraded
    raw = log.path.read_bytes()
    assert raw.count(b'\n') == 6
    assert b'\r\n' not in raw
    reloaded = EffectLog.load('b' * 32, directory=tmp_path)
    assert len(reloaded.history().claims) == len(reloaded.history().outcomes) == 3


def test_existing_crlf_journals_can_continue_without_rewriting_history(tmp_path):
    log = EffectLog('c' * 32, directory=tmp_path)
    log.claim(effect_id='original', action_id='action-original', operation=OperationRef('ls', '', '0' * 64))
    # Existing Windows log bytes remain readable; appending uses physical offsets.
    old_bytes = log.path.read_bytes().replace(b'\n', b'\r\n')
    log.path.write_bytes(old_bytes)
    reloaded = EffectLog.load('c' * 32, directory=tmp_path)
    reloaded.claim(effect_id='next', action_id='action-next', operation=OperationRef('ls', '', '0' * 64))
    assert reloaded.path.read_bytes().startswith(old_bytes)
    assert reloaded._offset == reloaded.path.stat().st_size
    assert len(reloaded.history().claims) == 2
