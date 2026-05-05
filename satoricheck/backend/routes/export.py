"""
Export routes.
Handles data export functionality.
"""
from flask import Blueprint, request, jsonify, make_response
import csv
import io
import json
import logging
from datetime import datetime, UTC

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
        
        # Get filters
        format_type = request.args.get('format', 'csv').lower()
        source_filter = request.args.get('source')
        
        # Get fact checks
        query = db_session.query(FactCheck).filter_by(user_id=user.id)
        if source_filter:
            query = query.filter_by(source=source_filter)
            
        fact_checks = query.order_by(FactCheck.timestamp.desc()).all()
        
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
                    except:
                        sources_str = sources_data
                elif isinstance(sources_data, list):
                    sources_str = '; '.join(sources_data)
                else:
                    sources_str = str(sources_data) if sources_data else ""

                writer.writerow([
                    fc.id,
                    fc.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                    getattr(fc, 'source', 'factcheck'),
                    fc.source_id or '',
                    fc.claim_text,
                    fc.verdict,
                    fc.explanation,
                    fc.fallacy or '',
                    sources_str
                ])
                
            # Create response
            output.seek(0)
            
            filename = f"authenix_export_{datetime.now(UTC).strftime('%Y%m%d')}.csv"
            if source_filter:
                filename = f"authenix_{source_filter}_{datetime.now(UTC).strftime('%Y%m%d')}.csv"
                
            response = make_response(output.getvalue())
            response.headers['Content-Type'] = 'text/csv'
            response.headers['Content-Disposition'] = f'attachment; filename={filename}'
            
            logger.info(f"Exported {len(fact_checks)} fact checks as CSV for user {user.email}")
            return response
        
        else:
            raise APIError('Unsupported format. Use: csv', status_code=400)
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Export error: {e}", exc_info=True)
        raise APIError('Failed to export data')

