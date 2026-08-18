from amc_bot.cli import build_parser


def test_ambiguous_submit_retries_default_to_five():
    args = build_parser().parse_args([])
    assert args.ambiguous_submit_retries == 5
