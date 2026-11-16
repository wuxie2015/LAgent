"""Replay maintenance on a private copy, without executing conversation tools.

Example: venv/bin/python scripts/verify_memory_recovery.py --source memory
Only the copy is changed. Diagnostic logs omit conversation and model response text.
"""
import argparse
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.application.memory_manager import MemoryManager
from src.common.config import Settings
from src.common.logging_config import get_logger
from src.service.memory_extractor import ModelMemoryExtractor
from src.service.memory_store import JsonMemoryStore
from src.service.ollama import OllamaClient


class CountingExtractor(ModelMemoryExtractor):
    calls = 0

    def _extract(self, *args):
        self.calls += 1
        return super()._extract(*args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--passes', type=int, default=8)
    args = parser.parse_args()
    if args.passes < 1:
        parser.error('--passes must be positive')
    source = args.source.resolve()
    source_bytes = (source / 'memory.json').read_bytes()
    original_hash = hashlib.sha256(source_bytes).hexdigest()
    output = Path(tempfile.mkdtemp(prefix='lin-memory-recovery-'))
    copied = output / 'memory'
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns('.writer.lock'))
    # Use the exact snapshot inspected above, even if the live agent writes meanwhile.
    (copied / 'memory.json').write_bytes(source_bytes)
    logger = get_logger()
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(output / 'maintenance.log', encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    print('diagnostics:', output, flush=True)
    settings = Settings.from_env()
    extractor = CountingExtractor(OllamaClient(settings.ollama_host, settings.memory_model or settings.model,
                                             timeout=settings.memory_policy.maintenance_timeout, json_mode=True),
                                  settings.memory_policy.extraction_max_chars)
    with JsonMemoryStore(copied) as store:
        memory = MemoryManager('memory recovery verification', store, extractor, settings.memory_policy)
        originals = {t['id']: t['messages'] for t in memory.state.recent_turns}
        for index in range(args.passes):
            before = extractor.calls
            memory.maintain_safely()
            assert extractor.calls - before <= settings.memory_policy.maintenance_calls
            print(json.dumps({'pass': index + 1, 'calls': extractor.calls - before,
                              'recent': len(memory.state.recent_turns), 'archives': len(memory.state.archives),
                              'pending': len(memory.state.maintenance['pending_turn_ids']),
                              'error': memory.state.maintenance.get('last_error')}, ensure_ascii=False), flush=True)
            if not memory.state.maintenance['pending_turn_ids'] and not memory.state.maintenance['last_error_code']:
                break
        recovered = {t['id']: t['messages'] for t in memory.state.recent_turns}
        recovered.update({ref['turn_id']: store.load_archived_turn(ref)['messages'] for ref in memory.state.archives})
        assert all(recovered.get(tid) == messages for tid, messages in originals.items()), 'Original messages changed'
        assert len(memory.state.recent_turns) <= settings.memory_policy.recent_turns, 'Backlog remains'
        assert not memory.state.maintenance['pending_turn_ids'], 'Pending maintenance remains'
        assert not memory.state.maintenance['last_error_code'], 'Maintenance error remains'
        store.load().validate()
    assert hashlib.sha256((source / 'memory.json').read_bytes()).hexdigest() == original_hash, 'Source changed during verification'
    print('PASS: backlog recovered, original messages preserved, source unchanged, no tools executed', flush=True)


if __name__ == '__main__':
    main()
