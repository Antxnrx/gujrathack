"""Feature 7 — operator notes.

Notes are attributed and append-only in spirit: an edit updates ``updated_at`` and
is audited, and only the author can change their own. In a police deployment the
note is often the only record of *why* an operator escalated something, so an
unattributable or silently-editable note is worse than no note.

The same polymorphic ``(target_type, target_id)`` shape as bookmarks, for the same
reason: the thing worth annotating is as often a vehicle or a camera as an alert.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app import db
from backend.app.common import as_uuid, audit, paginate
from backend.app.schemas import NoteCreate, NoteUpdate
from backend.app.security import Operator, current_operator

router = APIRouter(prefix="/notes", tags=["notes"])


@router.get("")
async def list_notes(
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    operator_filter: Optional[str] = Query(default=None, alias="operator"),
    q: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Operator = Depends(current_operator),
) -> dict:
    rows = await db.fetch(
        """
        SELECT * FROM notes
        WHERE ($1::text IS NULL OR target_type = $1)
          AND ($2::uuid IS NULL OR target_id = $2)
          AND ($3::text IS NULL OR operator = $3)
          AND ($4::text IS NULL OR body ILIKE '%' || $4 || '%')
        ORDER BY created_at DESC LIMIT $5 OFFSET $6
        """,
        target_type,
        as_uuid(target_id, "target id") if target_id else None,
        operator_filter, q, limit + 1, offset,
    )
    return paginate(rows, limit, offset)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_note(body: NoteCreate, operator: Operator = Depends(current_operator)) -> dict:
    tid = as_uuid(body.target_id, "target id")
    row = await db.fetchrow(
        "INSERT INTO notes (target_type, target_id, body, operator) VALUES ($1, $2, $3, $4) "
        "RETURNING *",
        body.target_type, tid, body.body, operator.label,
    )
    await audit(operator, "note.create", target=tid,
                target_type=body.target_type, note_id=str(row["id"]))
    return row


@router.patch("/{note_id}")
async def update_note(
    note_id: str,
    body: NoteUpdate,
    operator: Operator = Depends(current_operator),
) -> dict:
    nid = as_uuid(note_id, "note id")
    existing = await db.fetchrow("SELECT operator FROM notes WHERE id = $1", nid)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such note")
    if existing["operator"] != operator.label and not operator.can("admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "A note can only be edited by its author")
    row = await db.fetchrow(
        "UPDATE notes SET body = $2, updated_at = NOW() WHERE id = $1 RETURNING *",
        nid, body.body.strip(),
    )
    await audit(operator, "note.update", target=nid, previous_author=existing["operator"])
    return row


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: str, operator: Operator = Depends(current_operator)) -> None:
    nid = as_uuid(note_id, "note id")
    existing = await db.fetchrow("SELECT operator, body FROM notes WHERE id = $1", nid)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such note")
    if existing["operator"] != operator.label and not operator.can("admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "A note can only be deleted by its author")
    # The body goes into the audit detail before the row disappears: deleting a note
    # is itself a recorded act, and "a note was deleted" without its content is not
    # a usable trail.
    await audit(operator, "note.delete", target=nid,
                author=existing["operator"], body=existing["body"])
    await db.execute("DELETE FROM notes WHERE id = $1", nid)
