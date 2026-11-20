"""
Export routes.
Handles data export functionality.
"""
from flask import Blueprint, request, make_response
import csv
import io
import json
import logging
import re
from datetime import datetime, UTC

from backend.database import db_session
from backend.models import FactCheck
from backend.routes.auth import login_required
from backend.error_handlers import APIError
from backend.extensions import limiter

logger = logging.getLogger(__name__)

export_bp = Blueprint('export', __name__, url_prefix='/api/export')

MAX_EXPORT_ROWS = 1000
_SOURCE_FILTER_RE = re.compile(r'^[A-Za-z0-9_-]{1,50}$')
_CSV_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r', '\n')


def _sanitize_csv_cell(value) -> str:
    """Prefix leading spreadsheet formula characters to neutralize CSV injection."""
    if value is None:
        return ''
    text = str(value)
    if text.startswith(_CSV_FORMULA_PREFIXES) or text.lstrip().startswith(('=', '+', '-', '@')):
        return f"'{text}"
    return text


@export_bp.route('/factchecks', methods=['GET'])
@login_required
@limiter.limit("10 per minute")
def export_fact_checks():
    """Export user's fact-check history as CSV."""
    try:
        user = request.current_user

        # Get filters
        format_type = request.args.get('format', 'csv').lower()
        source_filter = request.args.get('source')
        if source_filter is not None and source_filter != '':
            if not _SOURCE_FILTER_RE.fullmatch(source_filter):
                raise APIError('Invalid source filter', status_code=400)
        else:
            source_filter = None

        # Get bounded fact checks
        query = db_session.query(FactCheck).filter_by(user_id=user.id)
        if source_filter:
            query = query.filter_by(source=source_filter)

        fact_checks = query.order_by(FactCheck.timestamp.desc()).limit(MAX_EXPORT_ROWS).all()

        if format_type == 'csv':
            # Create CSV
            output = io.StringIO()
            writer = csv.writer(output)

            # Write header
            writer.writerow([
                'ID',
                'Date',
                'Source',
                'Source ID',
                'Claim Text',
                'Verdict',
                'Explanation',
                'Fallacy',
                'Sources',
            ])

            # Write data
            for fc in fact_checks:
                # Handle sources formatting
                sources_data = fc.sources
                if isinstance(sources_data, str) and (sources_data.startswith('[') or sources_data.startswith('{')):
                    try:
                        sources_list = json.loads(sources_data)
                        sources_str = '; '.join(sources_list) if isinstance(sources_list, list) else str(sources_list)
                    except Exception:
                        sources_str = sources_data
                elif isinstance(sources_data, list):
                    sources_str = '; '.join(sources_data)
                else:
                    sources_str = str(sources_data) if sources_data else ""

                writer.writerow([
                    fc.id,
                    fc.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                    _sanitize_csv_cell(getattr(fc, 'source', 'factcheck')),
                    _sanitize_csv_cell(fc.source_id or ''),
                    _sanitize_csv_cell(fc.claim_text),
                    _sanitize_csv_cell(fc.verdict),
                    _sanitize_csv_cell(fc.explanation),
                    _sanitize_csv_cell(fc.fallacy or ''),
                    _sanitize_csv_cell(sources_str),
                ])

            # Create response
            output.seek(0)

            filename = f"authenix_export_{datetime.now(UTC).strftime('%Y%m%d')}.csv"
            if source_filter:
                filename = f"authenix_{source_filter}_{datetime.now(UTC).strftime('%Y%m%d')}.csv"

            response = make_response(output.getvalue())
            response.headers['Content-Type'] = 'text/csv'
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'

            logger.info(f"Exported {len(fact_checks)} fact checks as CSV for user {user.email}")
            return response

        else:
            raise APIError('Unsupported format. Use: csv', status_code=400)

    except APIError:
        raise
    except Exception as e:
        logger.error(f"Export error: {e}", exc_info=True)
        raise APIError('Failed to export data')

