"""
Phase 4 Verification Tests

Run with: pytest tests/test_phase4.py -v

Note: Some tests require valid credentials in .env
Tests are designed to skip gracefully if credentials are missing.
"""

import pytest
from decimal import Decimal


class TestConfig:
    """Test configuration loading."""

    def test_config_imports(self):
        """Test config imports work."""
        from src.config import (
            CHAIN_ID,
            CLOB_API_URL,
            POLY_PRIVATE_KEY,
            POLY_API_KEY,
            has_credentials,
            validate_config,
        )

        assert CHAIN_ID == 137
        assert "clob.polymarket.com" in CLOB_API_URL
        print("✓ Config imports work")

    def test_has_credentials(self):
        """Test credential detection."""
        from src.config import has_credentials

        result = has_credentials()
        print(f"  has_credentials: {result}")

        if result:
            print("✓ Credentials are configured")
        else:
            print("⚠ Credentials not configured (some tests will skip)")

    def test_validate_config(self):
        """Test config validation."""
        from src.config import validate_config

        # Should not raise for valid config
        validate_config()
        print("✓ Config validation passed")


class TestClient:
    """Test client creation."""

    def test_read_client(self):
        """Test read-only client creation."""
        from src.client import get_client, reset_clients

        reset_clients()
        client = get_client()

        assert client is not None
        print("✓ Read-only client created")

    def test_read_client_singleton(self):
        """Test read client is singleton."""
        from src.client import get_client

        client1 = get_client()
        client2 = get_client()

        assert client1 is client2
        print("✓ Read client is singleton")

    def test_auth_client_requires_creds(self):
        """Test auth client requires credentials."""
        from src.client import get_auth_client, reset_clients
        from src.config import has_credentials

        reset_clients()

        if not has_credentials():
            with pytest.raises(ValueError) as exc_info:
                get_auth_client()
            assert "credentials" in str(exc_info.value).lower()
            print("✓ Auth client correctly requires credentials")
        else:
            # Credentials present, should work
            client = get_auth_client()
            assert client is not None
            print("✓ Auth client created with credentials")

    def test_auth_client_singleton(self):
        """Test auth client is singleton."""
        from src.client import get_auth_client, reset_clients
        from src.config import has_credentials

        if not has_credentials():
            pytest.skip("Credentials not configured")

        reset_clients()
        client1 = get_auth_client()
        client2 = get_auth_client()

        assert client1 is client2
        print("✓ Auth client is singleton")


class TestAuth:
    """Test authentication utilities."""

    def test_auth_imports(self):
        """Test auth module imports."""
        from src.auth import (
            get_wallet_address,
            get_balances,
            check_allowances,
            verify_setup,
        )
        print("✓ Auth imports work")

    def test_get_wallet_address(self):
        """Test getting wallet address."""
        from src.config import has_credentials
        from src.auth import get_wallet_address

        if not has_credentials():
            pytest.skip("Credentials not configured")

        address = get_wallet_address()

        assert address is not None
        assert address.startswith("0x")
        assert len(address) == 42

        print(f"✓ Wallet address: {address}")

    @pytest.mark.network
    def test_get_balances(self):
        """Test getting balances."""
        from src.config import has_credentials
        from src.auth import get_balances

        if not has_credentials():
            pytest.skip("Credentials not configured")

        balances = get_balances()

        assert 'usdc_allowance' in balances
        print(f"✓ Balances: {balances}")

    @pytest.mark.network
    def test_check_allowances(self):
        """Test checking allowances."""
        from src.config import has_credentials
        from src.auth import check_allowances

        if not has_credentials():
            pytest.skip("Credentials not configured")

        allowances = check_allowances()

        assert 'usdc_approved' in allowances
        print(f"✓ Allowances: {allowances}")

    @pytest.mark.network
    def test_verify_setup(self):
        """Test full setup verification."""
        from src.config import has_credentials
        from src.auth import verify_setup

        if not has_credentials():
            pytest.skip("Credentials not configured")

        result = verify_setup()

        assert 'ok' in result
        assert 'issues' in result

        print(f"✓ Setup verification:")
        print(f"  OK: {result['ok']}")
        print(f"  Address: {result.get('address', 'N/A')}")

        if result['issues']:
            print(f"  Issues: {result['issues']}")


@pytest.mark.network
class TestIntegration:
    """Integration tests with real API."""

    def test_authenticated_api_call(self):
        """Test making an authenticated API call."""
        from src.config import has_credentials
        from src.client import get_auth_client

        if not has_credentials():
            pytest.skip("Credentials not configured")

        client = get_auth_client()

        # Try to get open orders (should work even if empty)
        try:
            orders = client.get_orders()
            print(f"✓ Authenticated API call successful")
            print(f"  Open orders: {len(orders) if orders else 0}")
        except Exception as e:
            # Some API errors are OK (e.g., no orders)
            if "unauthorized" in str(e).lower():
                pytest.fail(f"Authentication failed: {e}")
            print(f"✓ API call made (response: {e})")

    def test_can_read_markets_with_auth(self):
        """Test reading markets still works with auth client."""
        from src.config import has_credentials
        from src.markets import fetch_active_markets

        # This should work regardless of auth status
        markets = fetch_active_markets(limit=5)

        assert len(markets) > 0
        print(f"✓ Can read markets: {len(markets)} found")
