"""Context budgeting with a labeled estimate or explicit local tokenizer.

The fallback charges one estimated token per UTF-8 byte of serialized
messages/tool schemas, plus 32 per message/schema and 32 for reply framing.
Unknown server templates/hidden prompts mean neither this heuristic nor an
opt-in local tokenizer count guarantees server occupancy or model capacity.
Nothing is downloaded. Output tokens and an explicit safety margin are reserved.
"""
import json


ESTIMATE_METHOD = 'UTF-8 bytes + framing (ESTIMATE, not a model tokenizer)'


class UsageLedger:
    """Sum one final server snapshot per API request, not per tool or chunk.

    Unknown fields remain None until reported. Sums with incomplete coverage are
    known subtotals, never inferred totals; per-field coverage accompanies them.
    Reasoning/cache are subsets, NOT added to completion/prompt a second time.
    """
    fields = ('prompt_tokens', 'completion_tokens', 'total_tokens',
              'reasoning_tokens', 'cached_tokens')

    def __init__(self):
        self.requests = 0
        self.incomplete_requests = 0
        self.totals = dict.fromkeys(self.fields, 0)
        self.reported = dict.fromkeys(self.fields, 0)

    def record(self, usage, *, complete):
        self.requests += 1
        self.incomplete_requests += int(not complete)
        for field in self.fields:
            value = (usage or {}).get(field)
            if value is not None:
                self.totals[field] += value
                self.reported[field] += 1

    def snapshot(self):
        return {'scope': 'live-only', 'requests': self.requests,
                'incomplete_requests': self.incomplete_requests,
                'tokens': {k: self.totals[k] if self.reported[k] else None for k in self.fields},
                'reported_requests': dict(self.reported),
                'partial': bool(self.incomplete_requests or any(
                    self.reported[k] < self.requests for k in self.fields[:3]))}


def context_usage(settings, messages, tools):
    """Measure transmitted input; tools=None when schemas are already in prompt."""
    window = settings['model'].get('context_window_tokens', 0)
    serialized = json.dumps(messages, ensure_ascii=False)
    schemas = json.dumps(tools, ensure_ascii=False) if tools is not None else ''
    if not window:
        return {'unit': 'characters', 'input_characters': len(serialized) + len(schemas),
                'input_budget_characters': settings['agent']['max_context_chars']}
    output = settings['model']['max_tokens']
    margin = settings['agent'].get('context_safety_margin_tokens', 1024)
    estimate = (len(serialized.encode('utf-8')) + len(schemas.encode('utf-8'))
                + 32 * (1 + len(messages) + len(tools or [])))
    from .tokenizer import get_counter
    counter = get_counter(settings['model'])
    local = {}
    method = ESTIMATE_METHOD
    if counter is not None:
        estimate = counter.count(messages, tools)
        local = {'local_input_tokens': estimate, 'tokenizer_fingerprint': counter.fingerprint}
        method = 'local tokenizer + supplied chat template (server hidden input/template compatibility unverified)'
    # This is still an estimate of SERVER occupancy, even when local count is exact.
    return {**local, 'unit': 'estimated_tokens', 'estimated_input_tokens': estimate,
            'input_budget_tokens': window - output - margin,
            'context_window_tokens': window, 'reserved_output_tokens': output,
            'safety_margin_tokens': margin, 'method': method}
