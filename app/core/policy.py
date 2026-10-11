"""Choose the next evidence by coverage gain within the remaining budget."""

MAX_MOMENTS = 3


def plan_next(state: dict) -> dict:
    candidates = sorted(state['candidates'], key=lambda c: (-c['gain'], c['cam']))
    result = {'candidates': candidates}
    if state['tools_left'] <= 0 or state['seconds_left'] <= 0:
        return {**result, 'stop_reason': 'budget_exhausted'}
    if state['aggregate'] in {'SUPPORTED', 'CONTRADICTED'}:
        return {**result, 'stop_reason': 'enough_evidence'}
    if not candidates or candidates[0]['gain'] <= 0:
        return {**result, 'stop_reason': 'no_new_info'}
    return {**result, 'cam': candidates[0]['cam'], 'stop_reason': None}
