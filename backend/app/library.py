"""Project/episode library, without a second editing document store.

``projects`` remains the episode's canonical edit document. These tables only
hold production metadata, episode membership/archive state and asset links.
All mutation receipts and document revisions use the existing transaction.
"""
from fractions import Fraction
import json
import uuid

from fastapi import APIRouter
from pydantic import Field, model_validator
from sqlalchemy import Column, Integer, String, Table, Text

from . import storage as db
from .analysis import AnalysisProblem
from .schema import Canvas, Document, Ratio, StrictModel

router = APIRouter(prefix="/api/v1")
productions = Table("productions", db.metadata,
    Column("id", String, primary_key=True), Column("name", String, nullable=False),
    Column("description", Text, nullable=False), Column("language", String, nullable=False),
    Column("canvas_json", Text, nullable=False), Column("fps_json", Text, nullable=False),
    Column("revision", Integer, nullable=False), Column("archived", Integer, nullable=False),
    Column("created_at", String, nullable=False), Column("updated_at", String, nullable=False))
episodes = Table("episodes", db.metadata,
    Column("project_id", String, primary_key=True), Column("production_id", String, nullable=False),
    Column("revision", Integer, nullable=False), Column("archived", Integer, nullable=False),
    Column("created_at", String, nullable=False), Column("updated_at", String, nullable=False))
production_assets = Table("production_assets", db.metadata,
    Column("production_id", String, primary_key=True), Column("asset_id", String, primary_key=True),
    Column("created_at", String, nullable=False))
asset_links = production_assets


def problem(code, message, status=409):
    raise AnalysisProblem(code, message, status)


class Request(StrictModel):
    request_id: str = Field(min_length=8, max_length=100)


class ProductionCreate(Request):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    language: str = Field(default="zh", min_length=1, max_length=50)
    canvas: Canvas = Field(default_factory=lambda: Canvas(width=640, height=360))
    fps: Ratio = Field(default_factory=lambda: Ratio(num=25, den=1))

    @model_validator(mode="after")
    def values_valid(self):
        if not self.name.strip() or not self.language.strip():
            raise ValueError("名称和语言不能只有空格")
        if not 1 <= Fraction(self.fps.num, self.fps.den) <= 120:
            raise ValueError("工程帧率支持 1–120 fps")
        return self


class ProductionPatch(Request):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    language: str | None = Field(default=None, min_length=1, max_length=50)
    canvas: Canvas | None = None
    fps: Ratio | None = None
    archived: bool | None = None

    @model_validator(mode="after")
    def values_valid(self):
        fields = self.model_fields_set - {"expected_revision", "request_id"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("请提供需要修改的字段，字段不能为 null")
        if self.name is not None and not self.name.strip():
            raise ValueError("名称不能只有空格")
        if self.language is not None and not self.language.strip():
            raise ValueError("语言不能只有空格")
        if self.fps is not None and not 1 <= Fraction(self.fps.num, self.fps.den) <= 120:
            raise ValueError("工程帧率支持 1–120 fps")
        return self


class EpisodeCreate(Request):
    name: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def name_valid(self):
        if self.name is not None and not self.name.strip():
            raise ValueError("名称不能只有空格")
        return self


class CopyRequest(EpisodeCreate):
    expected_revision: int = Field(ge=1)


class AttachRequest(Request):
    asset_ids: list[str] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def ids_valid(self):
        if any(not value or len(value) > 100 for value in self.asset_ids):
            raise ValueError("素材编号格式不正确")
        return self


class EpisodePatch(Request):
    expected_revision: int = Field(ge=1)
    expected_document_revision: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None

    @model_validator(mode="after")
    def values_valid(self):
        fields = self.model_fields_set & {"name", "archived"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("请提供需要修改的名称或归档状态，字段不能为 null")
        if self.name is not None:
            if not self.name.strip():
                raise ValueError("名称不能只有空格")
            if self.expected_document_revision is None:
                raise ValueError("改名需要当前工程修订号 expected_document_revision")
        return self


def get_production(c, production_id):
    row = c.execute(productions.select().where(productions.c.id == production_id)).first()
    if row is None:
        problem("NOT_FOUND", "项目不存在", 404)
    return row


def get_episode(c, project_id):
    row = c.execute(episodes.select().where(episodes.c.project_id == project_id)).first()
    if row is None:
        problem("NOT_FOUND", "剧集不存在", 404)
    return row


def project_ids_for_production(c, production_id):
    get_production(c, production_id)
    return list(c.execute(episodes.select().with_only_columns(episodes.c.project_id)
        .where(episodes.c.production_id == production_id)).scalars())


def _referenced_assets(document):
    return {clip["source"]["asset_id"] for clip in document.get("clips", [])}


def _attach_existing(c, production_id, asset_ids):
    """Internal linkage also permits an archived production's historical refs."""
    existing = set(c.execute(production_assets.select().with_only_columns(production_assets.c.asset_id)
        .where(production_assets.c.production_id == production_id)).scalars())
    available = set(c.execute(db.assets.select().with_only_columns(db.assets.c.id)).scalars())
    for asset_id in sorted(set(asset_ids) & available - existing):
        c.execute(production_assets.insert().values(production_id=production_id,
            asset_id=asset_id, created_at=db.now()))


def attach_assets(c, production_id, asset_ids):
    production = get_production(c, production_id)
    if production.archived:
        problem("PRODUCTION_ARCHIVED", "项目已归档，请先恢复后再添加素材")
    available = set(c.execute(db.assets.select().with_only_columns(db.assets.c.id)).scalars())
    missing = set(asset_ids) - available
    if missing:
        problem("NOT_FOUND", "待关联素材不存在", 404)
    _attach_existing(c, production_id, asset_ids)


def ensure_episode(c, project_id, document):
    """Map a newly-created legacy API edit and keep its source links additive."""
    row = c.execute(episodes.select().where(episodes.c.project_id == project_id)).first()
    if row is None:
        production_id = str(uuid.uuid4())
        stamp = db.now()
        c.execute(productions.insert().values(id=production_id, name=document["name"],
            description="", language="zh", canvas_json=db.dumps(document["canvas"]),
            fps_json=db.dumps(document["fps"]), revision=1, archived=0,
            created_at=stamp, updated_at=stamp))
        c.execute(episodes.insert().values(project_id=project_id, production_id=production_id,
            revision=1, archived=0, created_at=stamp, updated_at=stamp))
    else:
        production_id = row.production_id
    _attach_existing(c, production_id, _referenced_assets(document))
    return production_id


def ensure_project_writable(c, project_id):
    """Archive protects new edits/jobs; existing frozen jobs remain readable."""
    row = c.execute(episodes.select().where(episodes.c.project_id == project_id)).first()
    if row is None:
        # Legacy callers can create a document before registering its mapping.
        # Missing edit projects are still rejected by the canonical entry point.
        return
    if get_production(c, row.production_id).archived:
        problem("PRODUCTION_ARCHIVED", "项目已归档，请先恢复后再编辑或导出")
    if row.archived:
        problem("EPISODE_ARCHIVED", "剧集已归档，请先恢复后再编辑或导出")


def episode_value(c, row):
    project = c.execute(db.projects.select().where(db.projects.c.id == row.project_id)).first()
    if project is None:
        problem("NOT_FOUND", "剧集工程不存在", 404)
    document = json.loads(project.document_json)
    return {"project_id": row.project_id, "production_id": row.production_id,
        "name": document["name"], "revision": row.revision, "document_revision": project.revision,
        "archived": bool(row.archived), "canvas": document["canvas"], "fps": document["fps"],
        "duration_frames": document["duration_frames"],
        "created_at": row.created_at, "updated_at": max(row.updated_at, project.updated_at)}


def production_value(c, row, detail=False):
    members = c.execute(episodes.select().join(db.projects, episodes.c.project_id == db.projects.c.id)
        .where(episodes.c.production_id == row.id).order_by(episodes.c.created_at, episodes.c.project_id)).fetchall()
    # The joined columns preserve episode keys first; do not expose document JSON.
    values = [episode_value(c, member) for member in members]
    count = len(c.execute(production_assets.select().where(production_assets.c.production_id == row.id)).fetchall())
    result = {"id": row.id, "name": row.name, "description": row.description,
        "language": row.language, "canvas": json.loads(row.canvas_json), "fps": json.loads(row.fps_json),
        "revision": row.revision, "archived": bool(row.archived),
        "created_at": row.created_at, "updated_at": row.updated_at,
        "episode_count": len(values), "active_episode_count": sum(not value["archived"] for value in values),
        "asset_count": count}
    if detail:
        result["episodes"] = values
    return result


def _document(c, production_id, name, source=None):
    from .main import snapshot
    production = get_production(c, production_id)
    project_id = str(uuid.uuid4())
    document = dict(source) if source is not None else {
        "project_id": project_id, "name": name,
        "canvas": json.loads(production.canvas_json), "fps": json.loads(production.fps_json),
        "duration_frames": 1, "tracks": [{"id": kind, "kind": kind, "enabled": True}
            for kind in ("video", "audio", "subtitle")], "clips": [], "subtitles": []}
    document.update(project_id=project_id, name=name)
    document = Document.model_validate(document).model_dump()
    stamp = db.now()
    c.execute(db.projects.insert().values(id=project_id, revision=1,
        document_json=db.dumps(document), updated_at=stamp))
    snapshot(c, project_id, 1, document)
    c.execute(episodes.insert().values(project_id=project_id, production_id=production_id,
        revision=1, archived=0, created_at=stamp, updated_at=stamp))
    _attach_existing(c, production_id, _referenced_assets(document))
    return {"id": project_id, "revision": 1, "document": document}


def _response(c, production_id, project):
    return {"production": production_value(c, get_production(c, production_id), True), "project": project}


def _receipt(c, key, body):
    from .main import receipt
    return receipt(c, key, body.model_dump())


def _record(c, key, body, response):
    from .main import record_receipt
    record_receipt(c, key, body.model_dump(), response)
    return response


@router.get("/productions")
def list_productions(include_archived: bool = False):
    with db.engine.connect() as c:
        query = productions.select().order_by(productions.c.updated_at.desc(), productions.c.id)
        if not include_archived:
            query = query.where(productions.c.archived == 0)
        return {"productions": [production_value(c, row) for row in c.execute(query)]}


@router.get("/productions/{production_id}")
def production_detail(production_id: str):
    with db.engine.connect() as c:
        return production_value(c, get_production(c, production_id), True)


@router.post("/productions/{production_id}/assets")
def link_assets(production_id: str, body: AttachRequest):
    key = f"productions:{production_id}:assets:{body.request_id}"
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = _receipt(c, key, body)
        if previous is not None:
            return previous
        attach_assets(c, production_id, body.asset_ids)
        return _record(c, key, body, production_value(c, get_production(c, production_id), True))


@router.get("/library/context/{project_id}")
def library_context(project_id: str):
    from .main import safe_asset
    with db.engine.connect() as c:
        row = get_episode(c, project_id)
        production = get_production(c, row.production_id)
        links = set(c.execute(production_assets.select().with_only_columns(production_assets.c.asset_id)
            .where(production_assets.c.production_id == production.id)).scalars())
        return {"production": production_value(c, production, True), "episode": episode_value(c, row),
            "assets": [safe_asset(value) for asset_id, value in db.asset_map(c).items() if asset_id in links]}


@router.post("/productions")
def create_production(body: ProductionCreate):
    key = f"productions:create:{body.request_id}"
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = _receipt(c, key, body)
        if previous is not None:
            return previous
        production_id, stamp = str(uuid.uuid4()), db.now()
        c.execute(productions.insert().values(id=production_id, name=body.name,
            description=body.description, language=body.language, canvas_json=db.dumps(body.canvas.model_dump()),
            fps_json=db.dumps(body.fps.model_dump()), revision=1, archived=0, created_at=stamp, updated_at=stamp))
        project = _document(c, production_id, "第 1 集")
        return _record(c, key, body, _response(c, production_id, project))


@router.patch("/productions/{production_id}")
def patch_production(production_id: str, body: ProductionPatch):
    key = f"productions:{production_id}:patch:{body.request_id}"
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = _receipt(c, key, body)
        if previous is not None:
            return previous
        row = get_production(c, production_id)
        if row.revision != body.expected_revision:
            problem("REVISION_CONFLICT", "项目资料已有新版本，请刷新后再修改")
        fields = body.model_dump(exclude_unset=True)
        fields.pop("request_id"); fields.pop("expected_revision")
        if row.archived and any(key != "archived" for key in fields):
            problem("PRODUCTION_ARCHIVED", "项目已归档，请先恢复后再修改资料")
        updates = {f"{key}_json" if key in ("canvas", "fps") else key:
            db.dumps(value) if key in ("canvas", "fps") else int(value) if key == "archived" else value
            for key, value in fields.items()}
        c.execute(productions.update().where(productions.c.id == production_id).values(
            **updates, revision=row.revision + 1, updated_at=db.now()))
        return _record(c, key, body, production_value(c, get_production(c, production_id), True))


@router.post("/productions/{production_id}/episodes")
def create_episode(production_id: str, body: EpisodeCreate):
    key = f"productions:{production_id}:episode:{body.request_id}"
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = _receipt(c, key, body)
        if previous is not None:
            return previous
        production = get_production(c, production_id)
        if production.archived:
            problem("PRODUCTION_ARCHIVED", "项目已归档，请先恢复后再创建剧集")
        count = len(project_ids_for_production(c, production_id))
        project = _document(c, production_id, body.name or f"第 {count + 1} 集")
        return _record(c, key, body, _response(c, production_id, project))


@router.patch("/episodes/{project_id}")
def patch_episode(project_id: str, body: EpisodePatch):
    from .main import commit_document, get_project, project_value
    key = f"episodes:{project_id}:patch:{body.request_id}"
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = _receipt(c, key, body)
        if previous is not None:
            return previous
        row = get_episode(c, project_id)
        production = get_production(c, row.production_id)
        if row.revision != body.expected_revision:
            problem("REVISION_CONFLICT", "剧集资料已有新版本，请刷新后再修改")
        if production.archived:
            problem("PRODUCTION_ARCHIVED", "项目已归档，请先恢复项目后再修改剧集")
        project = project_value(get_project(c, project_id))
        if body.name is not None:
            ensure_project_writable(c, project_id)
            document = {**project["document"], "name": body.name}
            project = commit_document(c, project_id, document, body.expected_document_revision)
        updates = {"revision": row.revision + 1, "updated_at": db.now()}
        if body.archived is not None:
            updates["archived"] = int(body.archived)
        c.execute(episodes.update().where(episodes.c.project_id == project_id).values(**updates))
        response = _response(c, production.id, project)
        response["episode"] = episode_value(c, get_episode(c, project_id))
        return _record(c, key, body, response)


@router.post("/episodes/{project_id}/copy")
def copy_episode(project_id: str, body: CopyRequest):
    from .main import get_project
    key = f"episodes:{project_id}:copy:{body.request_id}"
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = _receipt(c, key, body)
        if previous is not None:
            return previous
        episode = get_episode(c, project_id)
        ensure_project_writable(c, project_id)
        source = get_project(c, project_id)
        if source.revision != body.expected_revision:
            problem("REVISION_CONFLICT", "剧集已有新版本，请保存并刷新后复制")
        document = json.loads(source.document_json)
        project = _document(c, episode.production_id,
            body.name or f"{document['name'][:197]} 副本", document)
        return _record(c, key, body, _response(c, episode.production_id, project))


@router.post("/productions/{production_id}/copy")
def copy_production(production_id: str, body: CopyRequest):
    from .main import get_project
    key = f"productions:{production_id}:copy:{body.request_id}"
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = _receipt(c, key, body)
        if previous is not None:
            return previous
        source = get_production(c, production_id)
        if source.revision != body.expected_revision:
            problem("REVISION_CONFLICT", "项目已有新版本，请刷新后复制")
        if source.archived:
            problem("PRODUCTION_ARCHIVED", "项目已归档，请先恢复后复制")
        new_id, stamp = str(uuid.uuid4()), db.now()
        c.execute(productions.insert().values(id=new_id, name=body.name or f"{source.name[:197]} 副本",
            description=source.description, language=source.language,
            canvas_json=source.canvas_json, fps_json=source.fps_json,
            revision=1, archived=0, created_at=stamp, updated_at=stamp))
        rows = c.execute(episodes.select().join(db.projects, episodes.c.project_id == db.projects.c.id)
            .where(episodes.c.production_id == production_id)
            .order_by(episodes.c.created_at, episodes.c.project_id)).fetchall()
        first_project = None
        for row in rows:
            document = json.loads(get_project(c, row.project_id).document_json)
            project = _document(c, new_id, document["name"], document)
            if row.archived:
                c.execute(episodes.update().where(episodes.c.project_id == project["id"]).values(archived=1))
            if first_project is None or (not row.archived and
                    get_episode(c, first_project["id"]).archived):
                first_project = project
        if first_project is None:
            first_project = _document(c, new_id, "第 1 集")
        links = c.execute(production_assets.select().with_only_columns(production_assets.c.asset_id)
            .where(production_assets.c.production_id == production_id)).scalars()
        _attach_existing(c, new_id, set(links))
        return _record(c, key, body, _response(c, new_id, first_project))
