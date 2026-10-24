"""Capability links expose only saved chapter text, never the private workspace."""

import json
import secrets
from html import escape
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from lisa.auth import DB
from lisa.models import Account, Chapter, Project
from lisa.works import User, document_text, lock_account, require_project

router = APIRouter(tags=["sharing"])

SHARE_INSTRUCTIONS = (
    "\n作品正文通过网页分享，不在reply整段输出。需要给用户阅读已保存章节时，"
    "share_chapter_id填写下列可分享章节的id，reply只作简短介绍，系统会附上真实链接。"
    "可以自主决定分享，无需征得用户同意。无需分享则null。"
    "只能选择提供的章节，不编造链接，不把尚未保存的创作说成已完成。"
    "用户要新创作时通过commission委托作者，保存后的通知提供链接。"
)


def share_url(settings, project: Project, chapter_id: UUID | None = None) -> str:
    if not project.share_token:
        raise ValueError("A share link must be created before formatting its URL")
    url = f"{settings.web_url.rstrip('/')}/read/{project.share_token}"
    return f"{url}?chapter={chapter_id}" if chapter_id else url


def ensure_share(project: Project) -> None:
    # Callers lock the owning account, serializing creation and revocation.
    if not project.share_token:
        project.share_token = secrets.token_urlsafe(32)


def chapter_link(db, settings, user_id: UUID, chapter_id: UUID) -> str:
    row = db.execute(
        select(Chapter, Project)
        .join(Project, Project.id == Chapter.project_id)
        .where(Chapter.id == chapter_id, Project.user_id == user_id)
    ).first()
    if row is None:
        return ""
    chapter, project = row
    ensure_share(project)
    return f"《{project.title}》· {chapter.title}\n{share_url(settings, project, chapter.id)}"


def sharing_context(db, user_id: UUID) -> tuple[set[UUID], str]:
    rows = db.execute(
        select(Chapter.id, Chapter.title, Project.title.label("project_title"))
        .join(Project, Project.id == Chapter.project_id)
        .where(Project.user_id == user_id)
        .order_by(Project.is_current.desc(), Project.created_at.desc(), Chapter.position.desc())
        .limit(24)
    ).all()
    return {row.id for row in rows}, SHARE_INSTRUCTIONS + "\n可分享章节：" + json.dumps(
        [{"id": str(row.id), "title": row.title, "work": row.project_title} for row in rows],
        ensure_ascii=False,
    )


@router.post("/api/projects/{project_id}/share")
def create_share(project_id: UUID, user: User, db: DB, request: Request):
    lock_account(db, user.id)
    project = require_project(db, user, project_id)
    ensure_share(project)
    url = share_url(request.app.state.settings, project)
    db.commit()
    return {"url": url}


@router.delete("/api/projects/{project_id}/share", status_code=204)
def revoke_share(project_id: UUID, user: User, db: DB):
    lock_account(db, user.id)
    project = require_project(db, user, project_id)
    project.share_token = None
    db.commit()


_CSS = """
:root{color-scheme:light;background:#f8f7f3;color:#283c35;
font-family:'Microsoft YaHei','PingFang SC',system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0}main{max-width:840px;margin:48px auto;padding:0 24px}
header{padding:0 0 24px;border-bottom:1px solid #e2e4db}header p,footer{color:#626c60}
h1,h2,.prose{font-family:'Noto Serif SC','Songti SC','SimSun',serif}
h1{font-size:28px;overflow-wrap:anywhere}h2{font-size:24px;text-align:center}
article{background:#fffefb;border:1px solid #e2e4db;border-radius:4px;
padding:32px 44px;margin:28px 0}.prose{white-space:pre-wrap;overflow-wrap:anywhere;
font-size:17px;line-height:2.2;color:#4f584c}nav{padding:16px 0}nav ol{padding-left:24px}
li{padding:8px 0}a{color:#405d4d;overflow-wrap:anywhere}a[aria-current]{font-weight:bold}
a:focus-visible{outline:2px solid #a5684f;outline-offset:4px}footer{font-size:13px;padding:24px 0}
@media(max-width:600px){main{margin:24px auto;padding:0 16px}article{padding:20px}
h1{font-size:24px}.prose{font-size:16px;line-height:2.1}}
"""


def _page(title: str, body: str, *, status_code=200) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        f"<title>{escape(title)} · LISA</title><style>{_CSS}</style></head>"
        f"<body><main>{body}<footer>LISA · 作品阅读</footer></main></body></html>",
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            ),
        },
    )


@router.get("/read/{token}", response_class=HTMLResponse)
def read_share(token: str, db: DB, chapter: str | None = None):
    def unavailable():
        return _page(
            "链接不可用",
            "<h1>链接不可用</h1><p>作品不存在，或分享链接已失效。</p>",
            status_code=404,
        )
    if len(token) != 43:
        return unavailable()
    project = db.scalar(
        select(Project).join(Account, Account.id == Project.user_id).where(
            Project.share_token == token, Account.active.is_(True)
        )
    )
    if project is None:
        return unavailable()
    chapters = db.scalars(
        select(Chapter).where(Chapter.project_id == project.id).order_by(Chapter.position)
    ).all()
    selected = next((item for item in chapters if str(item.id) == chapter), None)
    if chapter is not None and selected is None:
        return unavailable()
    selected = selected or (chapters[0] if chapters else None)
    body = f"<header><p>LISA · 分享作品</p><h1>{escape(project.title)}</h1>"
    body += "<p>只读 · 已保存正文，后续修改会同步显示</p></header>"
    if selected:
        body += f"<article><h2>{escape(selected.title)}</h2>"
        body += f'<div class="prose">{escape(document_text(selected.content))}</div></article>'
    else:
        body += "<article><p>这部作品还没有已保存的章节。</p></article>"
    if chapters:
        body += '<nav aria-label="章节目录"><h2>章节目录</h2><ol>'
        for item in chapters:
            current = ' aria-current="page"' if item.id == selected.id else ""
            body += (
                f'<li><a href="?chapter={item.id}"{current}>'
                f"{escape(item.title)}</a></li>"
            )
        body += "</ol></nav>"
    return _page(selected.title if selected else project.title, body)
