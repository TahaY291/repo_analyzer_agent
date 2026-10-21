from pydantic import field_validator
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def password_within_limit(cls, v: str) -> str:
        return _within_bcrypt_limit(v)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: EmailStr
    profile_image_url: str | None
    is_verified: bool
    created_at: datetime



def _within_bcrypt_limit(value: str) -> str:
    # bcrypt only handles 72 *bytes*; some characters take more than one byte.
    if len(value.encode()) > 72:
        raise ValueError("Password is too long")
    return value


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)

    @field_validator("password")
    @classmethod
    def password_within_limit(cls, v: str) -> str:
        return _within_bcrypt_limit(v)


class LoginOut(BaseModel):
    user: UserOut

class VerifyEmailIn(BaseModel):
    email: EmailStr
    otp: str = Field(pattern=r"^\d{6}$")


class ResendOtpIn(BaseModel):
    email: EmailStr