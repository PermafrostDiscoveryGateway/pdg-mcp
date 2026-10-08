import pytest

from arcticdata_mcp.auth import PasswordTokenVerifier


async def test_accepts_correct_password():
    v = PasswordTokenVerifier("s3cret")
    tok = await v.verify_token("s3cret")
    assert tok is not None and tok.client_id == "arcticdata-mcp-client"


@pytest.mark.parametrize("bad", ["", "s3cre", "s3cret ", "S3CRET"])
async def test_rejects_wrong_password(bad):
    assert await PasswordTokenVerifier("s3cret").verify_token(bad) is None


def test_requires_password():
    with pytest.raises(ValueError):
        PasswordTokenVerifier("")
