"""Strict wire shapes; validation is called only after security/role checks."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.auth_service import password_bytes


def valid_password(value, *, minimum=8):
    try:
        password_bytes(value, minimum=minimum)
    except InputError as exc:
        raise ValueError("invalid UTF-8 password length") from exc
    return value


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Body):
    username: StrictStr = Field(min_length=1, max_length=100)
    password: StrictStr = Field(min_length=1, max_length=72)

    @field_validator("password")
    @classmethod
    def check_password(cls, value):
        return valid_password(value, minimum=1)


class Assign(Body):
    assignee_id: StrictStr = Field(min_length=1, max_length=128)
    expected_version: StrictInt = Field(ge=0, le=9223372036854775806)


class Start(Body):
    expected_version: StrictInt = Field(ge=0, le=9223372036854775806)


class Close(Start):
    resolution_note: StrictStr = Field(max_length=4000)


class CreateUser(Body):
    username: StrictStr = Field(min_length=1, max_length=100)
    password: StrictStr = Field(max_length=72)
    role: Literal["FINANCE", "SUPERVISOR", "HANDLER", "ADMIN"]

    @field_validator("password")
    @classmethod
    def check_password(cls, value):
        return valid_password(value)


class Active(Body):
    active: StrictBool
    expected_version: StrictInt = Field(ge=0, le=9223372036854775806)


class Pagination(Body):
    page: int = Field(default=1, ge=1, le=1000000)
    page_size: int = Field(default=50, ge=1, le=100)


class RunQuery(Pagination):
    batch_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)


class ImportForm(Body):
    snapshot_id: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class PipelineForm(ImportForm):
    matching_rule_version: StrictStr = Field(min_length=1, max_length=100)


class Run(Body):
    batch_id: StrictStr = Field(min_length=1, max_length=128)
    matching_rule_version: StrictStr = Field(min_length=1, max_length=100)


class AuditQuery(Body):
    target_type: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    target_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    page: int = Field(default=1, ge=1, le=1000000)
    page_size: int = Field(default=50, ge=1, le=100)


DifferenceType = Literal["MISSING_CUSTOMER", "SUSPECTED_DUPLICATE", "COUNTRY_CONFLICT", "MULTIPLE_CANDIDATES", "INSUFFICIENT_QUANTITY", "PRICE_MISMATCH", "NO_CANDIDATE_IN_RANGE", "NON_PRODUCT_REVIEW"]


class ExportQuery(Body):
    format: Literal['csv'] = 'csv'
    type: DifferenceType | None = None
    status: Literal['PENDING', 'IN_PROGRESS', 'CLOSED'] | None = None
    assignee_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)


class DifferenceQuery(Body):
    type: DifferenceType | None = None
    status: Literal["PENDING", "IN_PROGRESS", "CLOSED"] | None = None
    assignee_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    page: int = Field(default=1, ge=1, le=1000000)
    page_size: int = Field(default=50, ge=1, le=100)
