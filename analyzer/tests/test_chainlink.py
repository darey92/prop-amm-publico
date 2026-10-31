from propamm_analyzer.ingest.chainlink import ANSWER_UPDATED_TOPIC, decode_answer


def _word(value: int) -> str:
    return format(value % (1 << 256), "064x")


def test_answer_updated_is_decoded_and_scaled() -> None:
    log = {
        "blockNumber": hex(53_247_547),
        "topics": [ANSWER_UPDATED_TOPIC, "0x" + _word(1_247_480_841_390_501_200), "0x" + _word(42)],
        "data": "0x" + _word(1_793_250_000),
    }
    answer = decode_answer(log, 18)
    assert answer["block"] == 53_247_547
    assert answer["round_id"] == "42"
    assert answer["updated_at"] == 1_793_250_000
    assert abs(answer["answer"] - 1.2474808413905012) < 1e-15


def test_negative_answers_keep_their_sign() -> None:
    log = {
        "blockNumber": hex(1),
        "topics": [ANSWER_UPDATED_TOPIC, "0x" + _word(-150_000_000), "0x" + _word(1)],
        "data": "0x" + _word(0),
    }
    assert decode_answer(log, 8)["answer"] == -1.5
