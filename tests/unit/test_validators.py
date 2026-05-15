"""Unit tests for shared UI validation helpers."""

from __future__ import annotations

from sp_rtk_base.ui.validators import (
    FieldDef,
    ValidationDict,
    is_non_empty,
    is_numeric,
    is_valid_port,
    numeric_validation,
    port_validation,
    required,
)


class TestIsNonEmpty:
    """Tests for is_non_empty validator."""

    def test_non_empty_string(self) -> None:
        assert is_non_empty("hello") is True

    def test_empty_string(self) -> None:
        assert is_non_empty("") is False

    def test_whitespace_only(self) -> None:
        assert is_non_empty("   ") is False

    def test_single_char(self) -> None:
        assert is_non_empty("a") is True

    def test_numeric_string(self) -> None:
        assert is_non_empty("123") is True


class TestIsValidPort:
    """Tests for is_valid_port validator."""

    def test_valid_port_min(self) -> None:
        assert is_valid_port("1") is True

    def test_valid_port_max(self) -> None:
        assert is_valid_port("65535") is True

    def test_valid_port_common(self) -> None:
        assert is_valid_port("8080") is True

    def test_port_zero(self) -> None:
        assert is_valid_port("0") is False

    def test_port_over_max(self) -> None:
        assert is_valid_port("65536") is False

    def test_port_negative(self) -> None:
        assert is_valid_port("-1") is False

    def test_port_non_numeric(self) -> None:
        assert is_valid_port("abc") is False

    def test_port_empty(self) -> None:
        assert is_valid_port("") is False

    def test_port_float(self) -> None:
        assert is_valid_port("80.5") is False


class TestIsNumeric:
    """Tests for is_numeric validator."""

    def test_digits(self) -> None:
        assert is_numeric("12345") is True

    def test_zero(self) -> None:
        assert is_numeric("0") is True

    def test_non_numeric(self) -> None:
        assert is_numeric("abc") is False

    def test_mixed(self) -> None:
        assert is_numeric("12abc") is False

    def test_empty(self) -> None:
        assert is_numeric("") is False

    def test_float(self) -> None:
        assert is_numeric("1.5") is False

    def test_negative(self) -> None:
        assert is_numeric("-1") is False


class TestValidationFactories:
    """Tests for validation rule factory functions."""

    def test_required_returns_dict(self) -> None:
        result: ValidationDict = required("Name")
        assert "Name is required" in result
        # The callable should be is_non_empty
        assert result["Name is required"]("hello") is True
        assert result["Name is required"]("") is False

    def test_port_validation_returns_dict(self) -> None:
        result: ValidationDict = port_validation()
        assert "Port is required" in result
        assert "Port must be 1-65535" in result
        assert result["Port is required"]("8080") is True
        assert result["Port must be 1-65535"]("8080") is True
        assert result["Port must be 1-65535"]("99999") is False

    def test_numeric_validation_returns_dict(self) -> None:
        result: ValidationDict = numeric_validation("Baud rate")
        assert "Baud rate is required" in result
        assert "Baud rate must be a number" in result
        assert result["Baud rate is required"]("115200") is True
        assert result["Baud rate must be a number"]("115200") is True
        assert result["Baud rate must be a number"]("abc") is False


class TestTypeAliases:
    """Tests for type alias usage."""

    def test_field_def_tuple(self) -> None:
        """FieldDef can be used as a tuple type."""
        field: FieldDef = ("host", "Host", "localhost", required("Host"))
        assert field[0] == "host"
        assert field[1] == "Host"
        assert field[2] == "localhost"
        assert isinstance(field[3], dict)
