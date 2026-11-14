from typing import Literal
from pydantic import BaseModel, Field, field_validator

# Languages that both Gnani Prisma v3.0 (STT) and Timbre (TTS) support.
LanguageCode = Literal["en-IN", "hi-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "ta-IN", "te-IN"]
LANGUAGE_NAMES = {
    "en-IN": "English", "hi-IN": "Hindi", "bn-IN": "Bengali", "gu-IN": "Gujarati", "kn-IN": "Kannada",
    "ml-IN": "Malayalam", "mr-IN": "Marathi", "pa-IN": "Punjabi", "ta-IN": "Tamil", "te-IN": "Telugu",
}


class Category(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    keywords: str = Field(default="", max_length=500)
    escalate: bool = False


class Document(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    text: str = Field(min_length=1, max_length=20000)


class VoiceSettings(BaseModel):
    """How the agent sounds. Older saved configurations get these defaults."""
    # "timbre-v3.0": one voice that speaks every language.
    # "timbre-v2.5": a native voice picked per reply language (lower latency).
    model: Literal["timbre-v3.0", "timbre-v2.5"] = "timbre-v3.0"
    voice: str = Field(default="Shivani", max_length=40)
    speed: float = Field(default=1.0, ge=0.85, le=1.15)


class BusinessConfig(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    greeting: str = Field(min_length=1, max_length=600)
    rules: str = Field(min_length=1, max_length=8000)
    categories: list[Category] = Field(min_length=1, max_length=20)
    documents: list[Document] = Field(default_factory=list, max_length=30)
    required_fields: list[str] = Field(default_factory=lambda: ["booking_id"], max_length=10)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    # Extra words Prisma should listen for (brand names, product models, local terms).
    keyterms: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("categories")
    @classmethod
    def unique_categories(cls, values):
        if len({c.name.casefold() for c in values}) != len(values):
            raise ValueError("Category names must be unique")
        return values

    @field_validator("keyterms")
    @classmethod
    def clean_keyterms(cls, values):
        cleaned = []
        for value in values:
            value = value.strip()
            if not value or len(value) > 50 or "<|" in value or "|>" in value:
                continue
            if value.casefold() not in {v.casefold() for v in cleaned}:
                cleaned.append(value)
        return cleaned


class AccessRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)


class DraftRequest(BaseModel):
    config: BusinessConfig
    revision: int = Field(ge=1)


class PublishRequest(BaseModel):
    revision: int = Field(ge=1)


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=12000)


class TurnRequest(TextRequest):
    revision: int = Field(ge=0)


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    language: Literal["auto", "hi-en"] | LanguageCode = "auto"
    voice: VoiceSettings | None = None


class Decision(BaseModel):
    reply: str = Field(min_length=1, max_length=1800)
    # Same reply written for the ear: IDs spelled out, numbers as words.
    spoken_reply: str | None = Field(default=None, max_length=2400)
    language: LanguageCode = "en-IN"
    category: str = Field(max_length=80)
    status: Literal["open", "needs_human", "resolved"]
    summary: str = Field(max_length=1500)
    fields: dict[str, str] = Field(default_factory=dict)
    source_ids: list[int] = Field(default_factory=list)

    @field_validator("language", mode="before")
    @classmethod
    def tolerate_language(cls, value):
        # Models sometimes answer "hi" or "Hindi"; normalise rather than fail the turn.
        if not isinstance(value, str):
            return "en-IN"
        value = value.strip()
        if value in LANGUAGE_NAMES:
            return value
        for code, name in LANGUAGE_NAMES.items():
            if value.casefold() in (code[:2], name.casefold()):
                return code
        return "en-IN"
