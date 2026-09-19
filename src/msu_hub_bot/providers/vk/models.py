"""The rendering subset of VK's wall schema; unknown provider fields are ignored."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator

PositiveID = Annotated[int, Field(gt=0, strict=True)]


def nonzero(value: int) -> int:
    if value == 0:
        raise ValueError("Owner ID cannot be zero")
    return value


OwnerID = Annotated[int, Field(strict=True), AfterValidator(nonzero)]


class VkModel(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class Image(VkModel):
    url: str = ""
    src: str = ""
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    type: str = ""


class Photo(VkModel):
    sizes: list[Image] = Field(default_factory=list, max_length=100)
    images: list[Image] = Field(default_factory=list, max_length=100)
    orig_photo: Image | None = None


class Video(VkModel):
    id: PositiveID
    owner_id: OwnerID
    title: str = "Видео"
    duration: int = Field(default=0, ge=0)
    is_private: StrictInt | StrictBool = False
    image: list[Image] = Field(default_factory=list, max_length=100)
    first_frame: list[Image] = Field(default_factory=list, max_length=100)


class VideoPlaylist(VkModel):
    id: int = Field(strict=True)
    owner_id: OwnerID
    title: str = "Подборка видео"
    count: int = Field(default=0, ge=0)


class GroupAttachment(VkModel):
    id: PositiveID
    text: str = ""
    status: str = ""
    size: int | None = Field(default=None, ge=0)


class Coordinates(VkModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False, strict=True)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False, strict=True)


class Place(VkModel):
    title: str = ""
    address: str = ""
    city: str = ""
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False, strict=True)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False, strict=True)
    is_deleted: StrictInt | StrictBool = False


class Geo(VkModel):
    coordinates: Annotated[str, Field(max_length=128)] | Coordinates | None = None
    place: Place | None = None
    showmap: StrictInt | StrictBool | None = None

    @property
    def point(self) -> Coordinates | None:
        if self.showmap == 0:
            return None
        if isinstance(self.coordinates, Coordinates):
            return self.coordinates
        if isinstance(self.coordinates, str):
            try:
                latitude, longitude = map(float, self.coordinates.split())
                return Coordinates(latitude=latitude, longitude=longitude)
            except ValueError, TypeError:
                pass
        place = self.place
        if place and not place.is_deleted and place.latitude is not None and place.longitude is not None:
            return Coordinates(latitude=place.latitude, longitude=place.longitude)
        return None


class Audio(VkModel):
    artist: str = ""
    title: str = "Аудио"


class Document(VkModel):
    id: PositiveID
    owner_id: OwnerID
    title: str = "Документ"
    url: str = ""
    size: int = Field(default=0, ge=0)
    ext: str = ""
    is_unsafe: StrictInt | StrictBool = False


class Link(VkModel):
    url: str
    title: str = ""
    photo: Photo | None = None


class Page(VkModel):
    view_url: str
    title: str = ""


class Answer(VkModel):
    text: str
    votes: int = Field(default=0, ge=0)


class Poll(VkModel):
    id: PositiveID
    owner_id: OwnerID
    question: str
    votes: int = Field(default=0, ge=0)
    answers: list[Answer] = Field(default_factory=list, max_length=100)
    photo: Photo | None = None


class Album(VkModel):
    id: PositiveID
    owner_id: OwnerID
    title: str = "Альбом"
    size: int = Field(default=0, ge=0)
    thumb: Photo | None = None


class Price(VkModel):
    text: str = ""


class Market(VkModel):
    id: PositiveID
    owner_id: OwnerID
    title: str = "Товар"
    price: Price = Field(default_factory=Price)
    thumb_photo: str = ""


class MarketAlbum(VkModel):
    id: PositiveID
    owner_id: OwnerID
    title: str = "Подборка товаров"
    count: int = Field(default=0, ge=0)
    photo: Photo | None = None


class Card(VkModel):
    link_url: str
    title: str = ""
    price: str = ""


class Cards(VkModel):
    cards: list[Card] = Field(max_length=100)


class Event(VkModel):
    id: PositiveID


class Source(VkModel):
    id: PositiveID
    # An absent privacy flag cannot establish that privileged-token data is public.
    is_closed: StrictInt | StrictBool
    deactivated: str | None = None


class Entity(VkModel):
    id: PositiveID
    name: str = ""
    first_name: str = ""
    last_name: str = ""


class Resolution(VkModel):
    type: Literal["user", "group", "page"]
    object_id: PositiveID


class Donut(VkModel):
    is_donut: StrictInt | StrictBool = False


class Copyright(VkModel):
    link: str = ""
    name: str = ""


class Post(VkModel):
    id: PositiveID
    owner_id: OwnerID
    date: int = Field(default=0, ge=0)
    text: str = Field(default="", max_length=100_000)
    access_key: str | None = None
    attachments: list[object] = Field(default_factory=list, max_length=100)
    copy_history: list["Post"] = Field(default_factory=list, max_length=10)
    friends_only: StrictInt | StrictBool = False
    is_deleted: StrictInt | StrictBool = False
    is_archived: StrictInt | StrictBool = False
    post_type: str = "post"
    donut: Donut = Field(default_factory=Donut)
    copyright: Copyright | None = None
    signer_id: int | None = None
    from_id: int | None = None
    geo: Geo | None = None

    @field_validator("from_id", "signer_id", mode="before")
    @classmethod
    def usable_author(cls, value: object) -> int | None:
        return value if type(value) is int and value != 0 else None

    @field_validator("geo", mode="before")
    @classmethod
    def usable_geo(cls, value: object) -> Geo | None:
        try:
            return Geo.model_validate(value) if value is not None else None
        except ValidationError:
            # Optional location changes must not hide an otherwise usable post.
            return None

    @property
    def is_public(self) -> bool:
        return not (
            self.access_key or self.friends_only or self.is_deleted or self.is_archived or self.donut.is_donut
        ) and self.post_type in {
            "post",
            "copy",
            "photo",
            "video",
            "clip",
        }


class Wall(VkModel):
    items: list[Post] = Field(max_length=100)
    profiles: list[Entity] = Field(default_factory=list, max_length=1000)
    groups: list[Entity] = Field(default_factory=list, max_length=1000)

    @field_validator("items", mode="before")
    @classmethod
    def usable_posts(cls, value: object) -> object:
        if not isinstance(value, list) or len(value) > 100:
            return value
        result = []
        for raw in value:
            try:
                post = Post.model_validate(raw)
            except ValidationError:
                continue
            if post.is_public:
                result.append(post)
        return result

    @field_validator("profiles", "groups", mode="before")
    @classmethod
    def usable_entities(cls, value: object) -> object:
        if not isinstance(value, list) or len(value) > 1000:
            return value
        result = []
        for raw in value:
            try:
                result.append(Entity.model_validate(raw))
            except ValidationError:
                continue
        return result

    @property
    def extended(self) -> dict[int, Entity]:
        return {p.id: p for p in self.profiles} | {-g.id: g for g in self.groups}
