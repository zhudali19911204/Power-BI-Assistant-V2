"""Persist safe, per-request LLM diagnostics alongside conversations."""
import json
import logging

from .database import encode, now, uid

logger = logging.getLogger(__name__)


def generation_recorder(db, provider, *, stage, attempt=1, message_id=None, model_id=None):
    def record(report):
        diagnostic = report.as_dict() | {
            'model': getattr(provider, 'model', ''),
            'profile_id': getattr(provider, 'profile_id', None),
        }
        db.execute('INSERT INTO llm_generations VALUES (?,?,?,?,?,?,?)', (
            uid(), message_id, model_id, stage, attempt, encode(diagnostic), now()))
        logger.info('LLM %s stage=%s attempt=%s diagnostics=%s', message_id or model_id,
                    stage, attempt, encode(diagnostic))
    return record


def generation_records(db, message_id):
    return [dict(row, diagnostic=json.loads(row['diagnostic'])) for row in db.all(
        'SELECT stage,attempt,diagnostic,created_at FROM llm_generations WHERE message_id=? '
        'ORDER BY created_at,rowid', (message_id,))]


def record_format_validation(db, message_id, stage, attempt, *, issues=(), correction_attempt=0):
    """Attach bounded protocol diagnostics; retain no raw model reply or failed values."""
    row = db.one('SELECT id,diagnostic FROM llm_generations WHERE message_id=? AND stage=? AND attempt=?',
                 (message_id, stage, attempt))
    if not row:
        return
    validation = {'ok': not issues, 'correction_attempt': correction_attempt, 'issues': list(issues)[:5]}
    diagnostic = json.loads(row['diagnostic']) | {'format_validation': validation}
    db.execute('UPDATE llm_generations SET diagnostic=? WHERE id=?', (encode(diagnostic), row['id']))
    logger.info('LLM %s stage=%s attempt=%s format_validation=%s', message_id, stage, attempt, encode(validation))
