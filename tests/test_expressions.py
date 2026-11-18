import pytest

from wft.expressions import ExpressionError, parse

CTX = {
    "inputs": {"account": "ACC1", "limits": {"max": 10}, "markets": ["CME", "ICE"], "blank": ""},
    "steps": {"krm22_raise": {"reference": "K-42"}},
    "item": {"name": "x"},
}


@pytest.mark.parametrize("source, expected", [
    ("{{ inputs.account }}", "ACC1"),
    ("{{ inputs.limits }}", {"max": 10}),
    ("{{ inputs.markets.1 }}", "ICE"),
    ("Withdraw {{ steps.krm22_raise.reference }} now", "Withdraw K-42 now"),
    ("{{ inputs.missing | default('none') }}", "none"),
    ("{{ inputs.blank | default(inputs.account) }}", "ACC1"),
    ("{{ inputs.markets | join(' / ') }}", "CME / ICE"),
    ("{{ inputs.markets | join }}", "CME, ICE"),
    ("{{ inputs.account | lower }}", "acc1"),
    ("{{ inputs.markets | length }}", 2),
    ("{{ inputs.account == 'ACC1' }}", True),
    ("{{ inputs.limits.max != 10 }}", False),
    ("{{ item.name | upper }}", "X"),
    ("no holes", "no holes"),
    ("{{ inputs.missing }} left", " left"),
])
def test_render(source, expected):
    assert parse(source).render(CTX) == expected


@pytest.mark.parametrize("source, message", [
    ("{{ __import__('os') }}", "Unexpected|Unknown name"),
    ("{{ inputs.account.__class__ }}", "Unexpected"),
    ("{{ config.secret }}", "Unknown name 'config'"),
    ("{{ inputs.x | eval }}", "Unknown filter 'eval'"),
    ("{{ inputs.x | default }}", "takes 1-1"),
    ("{{ inputs.x | upper('a') }}", "takes 0-0"),
    ("{{ inputs.x + 1 }}", "Unexpected"),
    ("{{ }}", "Empty"),
    ("{{ inputs.x ", "Unbalanced"),
    ("{{ inputs.x == }}", "Expected"),
])
def test_rejects_anything_outside_the_subset(source, message):
    with pytest.raises(ExpressionError, match=message):
        parse(source)


def test_paths_are_reported_for_validation():
    t = parse("{{ steps.a.ref | default(inputs.b) }} and {{ item.c == 'x' }}")
    assert [p.parts for p in t.paths] == [("steps", "a", "ref"), ("inputs", "b"), ("item", "c")]
