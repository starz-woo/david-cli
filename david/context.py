"""Dependency-free context budgeting, NOT an exact model tokenizer.

The token heuristic charges one estimated token per UTF-8 byte of serialized
messages/tool schemas, plus 32 per message/schema and 32 for reply framing.
This deliberately overestimates ordinary byte-based tokenizers rather than
assuming a model-specific characters/token ratio. JSON escaping is included.
Unknown server chat templates, hidden prompts and tokenizer behavior mean this
is NOT a guaranteed upper bound or proof of the configured window's support.
Exact accounting requires the serving tokenizer/template; none is downloaded
or discovered here. Output tokens and an explicit safety margin are reserved.
"""
import json


ESTIMATE_METHOD = 'UTF-8 bytes + framing (ESTIMATE, not a model tokenizer)'


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
    return {'unit': 'estimated_tokens', 'estimated_input_tokens': estimate,
            'input_budget_tokens': window - output - margin,
            'context_window_tokens': window, 'reserved_output_tokens': output,
            'safety_margin_tokens': margin, 'method': ESTIMATE_METHOD}
