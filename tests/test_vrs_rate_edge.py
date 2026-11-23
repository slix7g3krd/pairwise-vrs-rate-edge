import pytest
import graphics.vrs_rate_edge as mod

CASES = [
    ("edge_rate", (1)),
    ("motion_mask", (1)),
    ("frame_fence", (1)),
    ("sample_consistency", (1)),
    ("capability_query", (1)),
    ("rate_determinism", (1)),
    ("shading_rate", (1)),
    ("validation", (1)),
    ("audit", (1)),
    ("fallback", (1)),
    ("hysteresis", (1)),
    ("metrics", (1)),
    ("msaa_path", (1)),
    ("debug_view", (1)),
    ("determinism", (1)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
