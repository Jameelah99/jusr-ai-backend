from pydantic import BaseModel, Field

class PatientCreate(BaseModel):
    displayLabel: str = Field(min_length=1, max_length=120)
    ageYears: int | None = Field(default=None, ge=0, le=130)
    sex: str | None = Field(default=None, max_length=30)
    birthYear: int | None = Field(default=None, ge=1850, le=2200)
