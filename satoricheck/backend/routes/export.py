"""
Export routes.
Handles data export functionality.
"""
from flask import Blueprint, request, jsonify, make_response
import csv
import io
import json
import logging
from datetime import datetime

from backend.database import db_session
from backend.models import FactCheck
from backend.routes.auth import login_required
from backend.error_handlers import APIError

logger = logging.getLogger(__name__)

export_bp = Blueprint('export', __name__, url_prefix='/api/export')


@export_bp.route('/factchecks', methods=['GET'])
@login_required
def export_fact_checks():
    """Export user's fact-check history as CSV."""
    try:
        user = request.current_user
        
        # Get format (default to csv)
        format_type = request.args.get('format', 'csv').lower()
        
        # Get fact checks
        fact_checks = db_session.query(FactCheck).filter_by(
            user_id=user.id
        ).order_by(
            FactCheck.timestamp.desc()
        ).all()
        
        if format_type == 'csv':
            # Create CSV
            output = io.StringIO()
            writer = csv.writer(output)
            
            # Write header
            writer.writerow([
                'ID',
                'Date',
                'Claim Text',
                'Verdict',
                'Explanation',
                'Fallacy',
                'Sources',
                            ])

            # Write data
            for fc in fact_checks:
                sources = json.loads(fc.sources) if fc.sources else []
                sources_str = '; '.join(sources)

                writer.writerow([
                    fc.id,
                    fc.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                    fc.claim_text,
                    fc.verdict,
                    fc.explanation,
                    fc.fallacy or '',
                    sources_str
                ])
            # Create response
            output.seek(0)
            response = make_response(output.getvalue())
            response.headers['Content-Type'] = 'text/csv'
            response.headers['Content-Disposition'] = f'attachment; filename=satoricheck_factchecks_{datetime.utcnow().strftime("%Y%m%d")}.csv'
            
            logger.info(f"Exported {len(fact_checks)} fact checks as CSV for user {user.email}")
            
            return response
        
        else:
            raise APIError('Unsupported format. Use: csv', status_code=400)
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Export error: {e}", exc_info=True)
        raise APIError('Failed to export data')

