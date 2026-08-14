from evidence_evolve.hashing import sha256_object


def test_set_order_is_canonical() -> None:
    left = {"permissions": {"CLAIM", "DEV", "TRAIN"}}
    right = {"permissions": {"TRAIN", "CLAIM", "DEV"}}
    assert sha256_object(left) == sha256_object(right)

