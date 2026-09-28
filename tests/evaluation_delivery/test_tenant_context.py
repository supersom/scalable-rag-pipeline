import asyncio

import pytest


@pytest.mark.parametrize('tenant, expected', [(None, None), ('../secret', None),
    ('x' * 129, None), (12, None), (['tenant'], None), ('tenant-one', 'tenant-one'),
    ('tenant-two', 'tenant-two')])
def test_signed_optional_tenant_does_not_change_auth(terminal_event, tenant, expected):
    from jose import jwt
    from services.api.app.auth.jwt import get_current_user
    from services.api.app.config import settings
    token = jwt.encode({'sub': 'synthetic-user', 'tenant_id': tenant},
                       settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    user = asyncio.run(get_current_user(token))
    assert user['id'] == 'synthetic-user'
    assert user['tenant_id'] == expected


def test_forged_tenant_token_rejected(terminal_event):
    from fastapi import HTTPException
    from jose import jwt
    from services.api.app.auth.jwt import get_current_user
    from services.api.app.config import settings
    token = jwt.encode({'sub': 'user', 'tenant_id': 'forged'}, 'wrong-key', algorithm=settings.JWT_ALGORITHM)
    with pytest.raises(HTTPException):
        asyncio.run(get_current_user(token))
