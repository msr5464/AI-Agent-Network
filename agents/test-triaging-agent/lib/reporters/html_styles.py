"""
CSS styles for HTML report generation.
Extracted from report_generator.py for better maintainability.
"""


def get_html_styles(c_success: str, c_warning: str, c_danger: str, c_info: str, c_text: str, c_light: str) -> str:
    """
    Generate CSS styles for the HTML report.
    
    Args:
        c_success: Success color (e.g., "#28a745")
        c_warning: Warning color (e.g., "#ffc107")
        c_danger: Danger color (e.g., "#dc3545")
        c_info: Info color (e.g., "#17a2b8")
        c_text: Text color (e.g., "#333333")
        c_light: Light background color (e.g., "#f8f9fa")
        
    Returns:
        CSS styles as a string
    """
    return f"""
/* Reset & Base */
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.5; color: #333333; margin: 0; padding: 0; background-color: #f4f6f9; }}
                .container {{ max-width: 1400px; margin: 20px auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
                
                /* Header */
                .header {{ background: linear-gradient(135deg, #2c3e50 0%, #3498db 100%); color: white; padding: 17px 24px; text-align: center; }}
                .header-logo {{ height: 35px; width: auto; object-fit: contain; margin-bottom: 10px; display: block; margin-left: auto; margin-right: auto; }}
                .report-title {{ margin: 0; font-size: 28px; font-weight: 600; line-height: 1.3; }}
                .report-meta {{ margin-top: 6px; opacity: 0.9; font-size: 16px; line-height: 1.4; word-break: break-word; overflow-wrap: break-word; }}
                
                /* Dashboard Grid */
                .dashboard {{ display: flex; flex-wrap: wrap; padding: 14px 20px; gap: 14px; border-bottom: 1px solid #eee; }}
                .card {{ flex: 1; min-width: 200px; max-width: 100%; background: #f8f9fa; padding: 14px 17px; border-radius: 6px; text-align: center; border-top: 3px solid #ddd; word-break: break-word; overflow-wrap: break-word; }}
                .card.success {{ border-color: #28a745; }}
                .card.danger {{ border-color: #dc3545; }}
                .card.info {{ border-color: #17a2b8; }}
                
                .metric-value {{ font-size: 29px; font-weight: 700; margin: 5px 0; line-height: 1.2; }}
                .metric-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: #666; font-weight: 600; }}
                .metric-detail {{ font-size: 13px; color: #495057; margin-top: 4px; font-weight: 500; line-height: 1.3; word-break: break-word; overflow-wrap: break-word; max-width: 100%; }}
                
                /* Progress Bars */
                .progress-section {{ padding: 20px 30px; }}
                .progress-label {{ display: flex; justify-content: space-between; margin-bottom: 5px; font-size: 14px; font-weight: 600; }}
                .progress-bg {{ height: 10px; background: #e9ecef; border-radius: 5px; overflow: hidden; }}
                .progress-bar {{ height: 100%; background: #28a745; }}
                
                /* Tables */
                .section {{ padding: 30px; }}
                .section-title {{ font-size: 18px; font-weight: 700; margin-bottom: 15px; border-left: 4px solid #3498db; padding-left: 10px; }}
                
                table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 14px; table-layout: fixed; }}
                th {{ text-align: left; padding: 12px; background: #f8f9fa; border-bottom: 2px solid #dee2e6; color: #495057; }}
                td {{ padding: 12px; border-bottom: 1px solid #dee2e6; vertical-align: top; word-wrap: break-word; word-break: break-word; overflow-wrap: break-word; min-width: 0; }}
                tr:last-child td {{ border-bottom: none; }}
                
                .badge {{ padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; color: white; display: inline-block; }}
                .badge-high {{ background: #dc3545; }}
                .badge-medium {{ background: #ffc107; color: #333; }}
                .badge-low {{ background: #17a2b8; }}
                
                .test-name {{ font-weight: 400; color: #2c3e50; display: block; margin-bottom: 4px; font-family: monospace; font-size: 13px; word-break: break-word; overflow-wrap: break-word; max-width: 100%; }}
                .test-description {{ color: #666; font-size: 12px; margin: 4px 0; font-style: italic; }}
                .test-confidence {{ display: inline-block; margin-top: 4px; }}
                .test-link {{ font-size: 11px; color: #3498db; text-decoration: none; margin-top: 4px; display: inline-block; }}
                .root-cause {{ color: #666; font-size: 13px; word-break: break-word; overflow-wrap: break-word; max-width: 100%; }}
                .action {{ color: #28a745; font-size: 12px; margin-top: 4px; font-style: italic; word-break: break-word; overflow-wrap: break-word; max-width: 100%; }}
                
                /* Executive Summary */
                .exec-summary {{ background: #eef2f7; padding: 25px; border-radius: 8px; font-size: 16px; line-height: 1.7; word-break: break-word; overflow-wrap: break-word; }}
                .exec-summary ul {{ margin: 10px 0; padding-left: 20px; }}
                .exec-summary li {{ margin-bottom: 8px; }}
                .exec-summary b {{ color: #2c3e50; }}
                
                /* Section Background Colors */
                .section-content {{ min-width: 0; max-width: 100%; }}
                .section-content.product-bugs {{ background: #fff5f5; padding: 20px; border-radius: 8px; border-left: 4px solid #dc3545; }}
                .section-content.product-changes {{ background: #f8f4ff; padding: 20px; border-radius: 8px; border-left: 4px solid #9b59b6; }}
                .section-content.automation-issues {{ background: #fffbf0; padding: 20px; border-radius: 8px; border-left: 4px solid #ffc107; }}
                .section-content.recurring-failures {{ background: #ffffff; padding: 16px; border-radius: 12px; border: 1px solid #e1e8ed; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
                .section-content.recurring-failures table {{ margin-bottom: 0; table-layout: auto; width: 100%; border-collapse: collapse; }}
                .section-content.recurring-failures thead {{ background-color: #f8f9fa; border-bottom: 2px solid #dee2e6; }}
                .section-content.known-failures {{ background: #ffffff; padding: 16px; border-radius: 12px; border: 1px solid #ffe0b2; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
                .section-content.known-failures table {{ margin-bottom: 0; table-layout: auto; width: 100%; border-collapse: collapse; }}
                .section-content.known-failures thead {{ background-color: #fff3e0; border-bottom: 2px solid #ffcc80; }}
                .section-content.known-failures th {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 13px; font-weight: 600; padding: 10px 14px; text-align: left; color: #e65100; text-transform: none; letter-spacing: 0; border: none; }}
                .section-content.known-failures th:first-child {{ padding-left: 14px; max-width: 300px; }}
                .section-content.known-failures th:nth-child(2) {{ padding: 10px 14px; width: 1px; white-space: nowrap; }}
                .section-content.known-failures th:nth-child(3) {{ padding-right: 14px; width: 1px; white-space: nowrap; }}
                .section-content.known-failures tbody tr {{ transition: background-color 0.15s ease; border-bottom: 1px solid #ffe0b2; background-color: #ffffff; }}
                .section-content.known-failures tbody tr:nth-child(even) {{ background-color: #fffbf7; }}
                .section-content.known-failures tbody tr:hover {{ background-color: #fff3e0; }}
                .section-content.known-failures tbody tr:last-child {{ border-bottom: none; }}
                .section-content.known-failures td {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 12px; padding: 10px 14px; text-align: left; vertical-align: middle; border: none; word-break: break-word; overflow-wrap: break-word; min-width: 0; }}
                .section-content.known-failures td:first-child {{ padding-left: 14px; max-width: 300px; }}
                .section-content.known-failures td:nth-child(2) {{ padding: 10px 14px; width: 1px; white-space: nowrap; }}
                .section-content.known-failures td:nth-child(3) {{ padding-right: 14px; width: 1px; white-space: nowrap; }}
                .section-content.known-failures .test-name {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 12px; color: #212529; font-weight: 500; line-height: 1.4; word-break: break-word; overflow-wrap: break-word; }}
                .section-content.known-failures .history-dots-container {{ display: flex; align-items: center; gap: 4px; flex-wrap: nowrap; }}
                .section-content.known-failures .history-dot {{
                    display: inline-block;
                    width: 14px;
                    height: 14px;
                    border-radius: 50%;
                    margin-right: 3px;
                    vertical-align: middle;
                    cursor: pointer;
                    transition: transform 0.2s ease, box-shadow 0.2s ease;
                    border: 1px solid rgba(0, 0, 0, 0.1);
                }}
                .section-content.known-failures .history-dot:hover {{
                    transform: scale(1.2);
                    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
                    z-index: 10;
                    position: relative;
                }}
                .section-content.known-failures .execution-details-row {{ background-color: #fffbf7; }}
                .section-content.known-failures .execution-details-content {{ padding: 16px; background-color: #f8f9fa; border-left: 3px solid #ff9800; margin: 12px 14px; border-radius: 6px; position: relative; }}
                .section-content.recurring-failures th {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 13px; font-weight: 600; padding: 10px 14px; text-align: left; color: #495057; text-transform: none; letter-spacing: 0; border: none; }}
                .section-content.recurring-failures th:first-child {{ padding-left: 14px; max-width: 300px; }}
                .section-content.recurring-failures th:nth-child(2) {{ padding: 10px 14px; width: 1px; white-space: nowrap; }}
                .section-content.recurring-failures th:nth-child(3) {{ padding-right: 14px; width: 1px; white-space: nowrap; }}
                .section-content.recurring-failures tbody tr {{ transition: background-color 0.15s ease; border-bottom: 1px solid #f1f3f5; background-color: #ffffff; }}
                .section-content.recurring-failures tbody tr:nth-child(even) {{ background-color: #fafbfc; }}
                .section-content.recurring-failures tbody tr:hover {{ background-color: #f0f4ff; }}
                .section-content.recurring-failures tbody tr:last-child {{ border-bottom: none; }}
                .section-content.recurring-failures td {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 12px; padding: 10px 14px; text-align: left; vertical-align: middle; border: none; word-break: break-word; overflow-wrap: break-word; min-width: 0; }}
                .section-content.recurring-failures td:first-child {{ padding-left: 14px; max-width: 300px; }}
                .section-content.recurring-failures td:nth-child(2) {{ padding: 10px 14px; width: 1px; white-space: nowrap; }}
                .section-content.recurring-failures td:nth-child(3) {{ padding-right: 14px; width: 1px; white-space: nowrap; }}
                .section-content.recurring-failures .test-name {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 12px; color: #212529; font-weight: 500; line-height: 1.4; word-break: break-word; overflow-wrap: break-word; }}
                .section-content.recurring-failures .history-dots-container {{ display: flex; align-items: center; gap: 4px; flex-wrap: nowrap; }}
                .section-content.recurring-failures .history-dot {{
                    display: inline-block;
                    width: 14px;
                    height: 14px;
                    border-radius: 50%;
                    margin-right: 3px;
                    vertical-align: middle;
                    cursor: pointer;
                    transition: transform 0.2s ease, box-shadow 0.2s ease;
                    border: 1px solid rgba(0, 0, 0, 0.1);
                }}
                .section-content.recurring-failures .history-dot:hover {{
                    transform: scale(1.2);
                    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
                    z-index: 10;
                    position: relative;
                }}
                .section-content.recurring-failures .pattern-badge {{ display: inline-block; padding: 4px 10px; border-radius: 6px; font-weight: 500; font-size: 11px; letter-spacing: 0; white-space: nowrap; }}
                .section-content.recurring-failures .pattern-badge.pattern-critical {{
                    background-color: #fee2e2;
                    color: #991b1b;
                    border: 1px solid #fecaca;
                }}
                .section-content.recurring-failures .pattern-badge.pattern-high {{
                    background-color: #fed7aa;
                    color: #9a3412;
                    border: 1px solid #fdba74;
                }}
                .section-content.recurring-failures .pattern-badge.pattern-medium {{
                    background-color: #fef3c7;
                    color: #854d0e;
                    border: 1px solid #fde68a;
                }}
                .section-content.recurring-failures .pattern-badge.pattern-low {{
                    background-color: #e0f2fe;
                    color: #0c4a6e;
                    border: 1px solid #bae6fd;
                }}
                .section-content.recurring-failures .execution-detail {{
                    margin-top: 8px;
                    padding: 12px;
                    background-color: #f8f9fa;
                    border-left: 3px solid #495057;
                    border-radius: 6px;
                    font-size: 12px;
                    line-height: 1.5;
                    word-break: break-word;
                    overflow-wrap: break-word;
                    min-width: 0;
                    max-width: 100%;
                }}
                .section-content.recurring-failures .execution-detail-header {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 8px;
                    padding-bottom: 8px;
                    border-bottom: 1px solid #dee2e6;
                }}
                .section-content.recurring-failures .execution-detail-header strong {{
                    font-weight: 600;
                    color: #495057;
                    font-size: 13px;
                }}
                .section-content.recurring-failures .execution-date {{
                    font-size: 11px;
                    color: #6c757d;
                    font-style: italic;
                }}
                .section-content.recurring-failures .execution-detail-close {{
                    background-color: #6c757d;
                    color: white;
                    border: none;
                    border-radius: 2px;
                    padding: 3px 7px;
                    cursor: pointer;
                    font-size: 10px;
                    font-weight: 600;
                    transition: background-color 0.2s ease;
                }}
                .section-content.recurring-failures .execution-detail-close:hover {{
                    background-color: #495057;
                }}
                .section-content.recurring-failures .execution-error {{
                    margin-top: 8px;
                    padding: 8px;
                    background-color: #fff5f5;
                    border-left: 3px solid #dc2626;
                    border-radius: 4px;
                    color: #991b1b;
                    font-size: 11px;
                    line-height: 1.5;
                    white-space: pre-wrap;
                    word-wrap: break-word;
                    overflow-wrap: break-word;
                }}
                .section-content.recurring-failures .execution-details-row {{
                    background-color: #f8f9fa;
                }}
                .section-content.recurring-failures .execution-details-content {{
                    padding: 16px;
                    background-color: #f8f9fa;
                    border-left: 3px solid #495057;
                    margin: 12px 14px;
                    border-radius: 6px;
                    position: relative;
                    word-break: break-word;
                    overflow-wrap: break-word;
                    min-width: 0;
                    max-width: 100%;
                }}
                .section-content.root-cause-categories-container {{ background: #ffffff; padding: 20px; border-radius: 8px; border: 1px solid #e5e7eb; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
                
                /* Root Cause Grid */
                .root-cause-subtitle {{ color: #6c757d; font-size: 13px; margin: 6px 0 18px; }}
                .root-cause-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 18px; }}
                .root-cause-grid-first-row {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 18px; }}
                .root-cause-grid-second-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 18px; margin-top: 18px; }}
                .root-cause-card {{
                    position: relative;
                    background: #fff;
                    border-radius: 14px;
                    padding: 18px 20px 20px;
                    border: 1px solid #eef2f7;
                    box-shadow: 0 15px 35px rgba(15, 23, 42, 0.08);
                    overflow: visible !important;
                    min-width: 0;
                    max-width: 100%;
                }}
                .root-cause-card::before {{
                    content: "";
                    position: absolute;
                    inset: 0;
                    background: var(--rc-gradient, linear-gradient(135deg, #f8f9fa, #fff));
                    opacity: 0.18;
                    pointer-events: none;
                }}
                .root-cause-card-content {{ position: relative; z-index: 1; display: flex; flex-direction: column; gap: 12px; }}
                .root-cause-card-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }}
                .root-cause-card-title {{ font-size: 15px; font-weight: 700; color: #1f2a37; display: flex; align-items: center; gap: 8px; min-width: 0; word-break: break-word; overflow-wrap: break-word; }}
                .root-cause-card-count {{ text-align: right; }}
                .root-cause-card-count span {{ display: block; }}
                .root-cause-card-count .count {{ font-size: 28px; font-weight: 700; color: var(--rc-color, #6610f2); line-height: 1; }}
                .root-cause-card-count .percent {{ font-size: 13px; color: #6c757d; }}
                .root-cause-meter {{ width: 100%; height: 8px; background: #e9ecef; border-radius: 999px; overflow: hidden; }}
                .root-cause-meter-fill {{ height: 100%; border-radius: inherit; background: var(--rc-color, #6610f2); }}
                .root-cause-meta {{ display: flex; justify-content: space-between; font-size: 13px; color: #5c6770; }}
                .root-cause-pill {{ display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
                .root-cause-note {{ font-size: 13px; color: #3b4151; background: rgba(255,255,255,0.6); border: 1px dashed rgba(0,0,0,0.06); border-radius: 8px; padding: 10px; line-height: 1.5; min-height: 54px; min-width: 0; word-break: break-word; overflow-wrap: break-word; }}
                .root-cause-note .root-cause-note-title + div {{ word-break: break-word; overflow-wrap: break-word; max-width: 100%; }}
                .root-cause-tests {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 6px;
                    position: relative;
                    z-index: 1;
                    width: 100%;
                    min-width: 0;
                    max-width: 100%;
                }}
                .root-cause-chip-container {{
                    display: inline-flex;
                    align-items: center;
                    gap: 4px;
                    padding: 4px 6px 4px 10px;
                    border-radius: 999px;
                    background: rgba(0, 0, 0, 0.05);
                    max-width: 100%;
                    position: relative;
                    z-index: 1;
                }}
                .root-cause-chip-container:hover {{ background: rgba(0, 0, 0, 0.08); }}
                .root-cause-chip-container.muted {{ background: rgba(148, 163, 184, 0.1); }}
                .root-cause-chip {{
                    font-size: 12px;
                    padding: 0;
                    border-radius: 0;
                    background: transparent;
                    color: #1f2933;
                    text-decoration: none;
                    display: inline-flex;
                    align-items: center;
                    gap: 4px;
                    font-weight: 500;
                    max-width: 100%;
                    word-break: break-word;
                    line-height: 1.3;
                    user-select: text;
                    -webkit-user-select: text;
                    -moz-user-select: text;
                    -ms-user-select: text;
                    flex: 1;
                    min-width: 0;
                    cursor: pointer;
                }}
                .root-cause-chip:hover {{ background: transparent; text-decoration: underline; }}
                .root-cause-chip.muted {{ color: #94a3b8; }}
                .root-cause-chip.muted:hover {{ text-decoration: none; }}
                .root-cause-copy-btn {{
                    background: rgba(99, 102, 241, 0.08);
                    border: 1px solid rgba(99, 102, 241, 0.2);
                    cursor: pointer;
                    padding: 3px 4px;
                    font-size: 10px;
                    color: #6366f1;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border-radius: 4px;
                    transition: all 0.2s;
                    flex-shrink: 0;
                    user-select: none;
                    -webkit-user-select: none;
                    -moz-user-select: none;
                    -ms-user-select: none;
                    position: relative;
                    z-index: 10000;
                    width: 16px;
                    height: 16px;
                    min-width: 16px;
                    min-height: 16px;
                }}
                .root-cause-copy-btn svg {{
                    width: 10px;
                    height: 10px;
                    fill: none;
                    stroke: currentColor;
                    stroke-width: 2.5;
                    stroke-linecap: round;
                    stroke-linejoin: round;
                }}
                .root-cause-link-btn {{
                    background: rgba(34, 197, 94, 0.08);
                    border: 1px solid rgba(34, 197, 94, 0.2);
                    cursor: pointer;
                    padding: 3px 4px;
                    font-size: 10px;
                    color: #22c55e;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border-radius: 4px;
                    transition: all 0.2s;
                    flex-shrink: 0;
                    user-select: none;
                    -webkit-user-select: none;
                    -moz-user-select: none;
                    -ms-user-select: none;
                    position: relative;
                    z-index: 10000;
                    width: 16px;
                    height: 16px;
                    min-width: 16px;
                    min-height: 16px;
                    text-decoration: none;
                }}
                .root-cause-link-btn:hover {{
                    background: rgba(34, 197, 94, 0.15);
                    border-color: rgba(34, 197, 94, 0.4);
                    color: #16a34a;
                    z-index: 10001;
                }}
                .root-cause-link-btn svg {{
                    width: 10px;
                    height: 10px;
                    fill: none;
                    stroke: currentColor;
                    stroke-width: 2.5;
                    stroke-linecap: round;
                    stroke-linejoin: round;
                }}
                .root-cause-copy-btn:hover {{
                    background: rgba(99, 102, 241, 0.15);
                    border-color: rgba(99, 102, 241, 0.4);
                    color: #4f46e5;
                    z-index: 10001;
                }}
                .root-cause-copy-btn:active {{
                    transform: scale(0.85);
                    background: rgba(34, 197, 94, 0.2);
                    border-color: rgba(34, 197, 94, 0.5);
                    color: #22c55e;
                }}
                .root-cause-copy-btn.copied {{
                    background: rgba(34, 197, 94, 0.25) !important;
                    border-color: rgba(34, 197, 94, 0.6) !important;
                    color: #16a34a !important;
                    transform: scale(1.1);
                    animation: copySuccess 0.4s ease-out;
                }}
                @keyframes copySuccess {{
                    0% {{
                        transform: scale(0.85) rotate(0deg);
                        background: rgba(34, 197, 94, 0.2);
                    }}
                    50% {{
                        transform: scale(1.15) rotate(5deg);
                        background: rgba(34, 197, 94, 0.35);
                    }}
                    100% {{
                        transform: scale(1.1) rotate(0deg);
                        background: rgba(34, 197, 94, 0.25);
                    }}
                }}
                .root-cause-copy-btn.copied svg {{
                    animation: checkmarkAppear 0.3s ease-out;
                }}
                @keyframes checkmarkAppear {{
                    0% {{
                        opacity: 0;
                        transform: scale(0) rotate(-90deg);
                    }}
                    50% {{
                        opacity: 1;
                        transform: scale(1.2) rotate(10deg);
                    }}
                    100% {{
                        opacity: 1;
                        transform: scale(1) rotate(0deg);
                    }}
                }}
                /* Tooltip container - ensure it's not clipped */
                .root-cause-chip-container {{
                    overflow: visible !important;
                }}
                .root-cause-copy-btn,
                .root-cause-link-btn {{
                    overflow: visible !important;
                }}
                /* Disable CSS pseudo-element tooltips - using JavaScript instead */
                .root-cause-copy-btn[title]:hover::after,
                .root-cause-link-btn[title]:hover::after {{
                    display: none !important;
                }}
                .root-cause-copy-btn[title]:hover::before,
                .root-cause-link-btn[title]:hover::before {{
                    display: none !important;
                }}
                .root-cause-chip-icon {{
                    font-size: 10px;
                    opacity: 0.6;
                    flex-shrink: 0;
                }}
                .root-cause-note-title {{ font-size: 11px; font-weight: 700; color: #4b5563; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }}
                .root-cause-note ul {{ margin: 0; padding-left: 18px; color: #1f2933; font-size: 12px; }}
                .root-cause-note li {{ margin-bottom: 4px; }}
                .root-cause-footnote {{ font-size: 11px; color: #94a3b8; margin-top: 12px; text-align: right; }}
                
                /* Expandable section for additional tests */
                .root-cause-expand-more {{
                    margin-top: 8px;
                    user-select: none;
                }}
                .root-cause-expand-summary {{
                    display: inline-flex;
                    align-items: center;
                    cursor: pointer;
                    color: #6366f1;
                    font-size: 12px;
                    font-weight: 600;
                    padding: 4px 8px;
                    border-radius: 4px;
                    transition: all 0.2s;
                    list-style: none;
                }}
                .root-cause-expand-summary::-webkit-details-marker {{
                    display: none;
                }}
                .root-cause-expand-summary::before {{
                    content: '▶';
                    display: inline-block;
                    margin-right: 6px;
                    font-size: 10px;
                    transition: transform 0.2s;
                }}
                .root-cause-expand-more[open] .root-cause-expand-summary::before {{
                    transform: rotate(90deg);
                }}
                .root-cause-expand-summary:hover {{
                    background: rgba(99, 102, 241, 0.1);
                    color: #4f46e5;
                }}
                .root-cause-expanded-tests {{
                    margin-top: 8px;
                    display: flex;
                    flex-wrap: wrap;
                    gap: 6px;
                    padding-left: 0;
                }}
                
                /* Test chip with expandable details */
                .test-chip-with-details {{
                    display: flex;
                    flex-direction: column;
                    gap: 4px;
                    margin-bottom: 6px;
                    width: 100%;
                    min-width: 0;
                    max-width: 100%;
                }}
                
                /* Expand icon at start of test name */
                .test-expand-icon {{
                    display: inline-block;
                    font-size: 10px;
                    color: #6366f1;
                    margin-right: 6px;
                    transition: transform 0.3s ease-in-out;
                    cursor: pointer;
                    user-select: none;
                    vertical-align: middle;
                    transform: rotate(0deg);
                    transform-origin: center;
                    pointer-events: none;
                }}
                .test-expand-icon.expanded {{
                    transform: rotate(90deg);
                }}
                .test-chip-with-details:has(.test-details-expandable[open]) .test-expand-icon {{
                    transform: rotate(90deg);
                }}
                .test-expand-icon:hover {{
                    color: #4f46e5;
                }}
                
                /* Expandable details section */
                .test-details-expandable {{
                    margin-top: 6px;
                    margin-left: 0;
                    width: 100%;
                    max-width: 100%;
                    min-width: 0;
                }}
                .test-details-summary {{
                    display: none;
                }}
                .test-details-content {{
                    margin-top: 8px;
                    padding: 12px;
                    padding-top: 32px;
                    background: rgba(255,255,255,0.95);
                    border-radius: 6px;
                    border: 1px solid rgba(99, 102, 241, 0.2);
                    font-size: 12px;
                    line-height: 1.6;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.05);
                    width: 100%;
                    max-width: 100%;
                    min-width: 0;
                    box-sizing: border-box;
                    word-wrap: break-word;
                    overflow-wrap: break-word;
                    word-break: break-word;
                    overflow-x: hidden;
                    position: relative;
                }}
                .test-details-close {{
                    position: absolute;
                    top: 8px;
                    right: 8px;
                    background: transparent;
                    border: none;
                    cursor: pointer;
                    padding: 4px;
                    color: #6b7280;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border-radius: 4px;
                    transition: all 0.2s;
                    width: 20px;
                    height: 20px;
                    z-index: 10;
                }}
                .test-details-close:hover {{
                    background: rgba(0, 0, 0, 0.1);
                    color: #1f2937;
                }}
                .test-details-close svg {{
                    width: 14px;
                    height: 14px;
                    stroke: currentColor;
                    stroke-width: 2.5;
                    stroke-linecap: round;
                    stroke-linejoin: round;
                }}
                .test-details-header-actions {{
                    position: absolute;
                    top: 8px;
                    left: 8px;
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    z-index: 10;
                }}
                .test-details-link-btn {{
                    background: rgba(34, 197, 94, 0.08);
                    border: 1px solid rgba(34, 197, 94, 0.2);
                    cursor: pointer;
                    padding: 4px;
                    color: #22c55e;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border-radius: 4px;
                    transition: all 0.2s;
                    width: 20px;
                    height: 20px;
                    min-width: 20px;
                    min-height: 20px;
                    text-decoration: none;
                }}
                .test-details-link-btn:hover {{
                    background: rgba(34, 197, 94, 0.15);
                    border-color: rgba(34, 197, 94, 0.4);
                    color: #16a34a;
                }}
                .test-details-link-btn svg {{
                    width: 12px;
                    height: 12px;
                    fill: none;
                    stroke: currentColor;
                    stroke-width: 2.5;
                    stroke-linecap: round;
                    stroke-linejoin: round;
                }}
                .test-details-copy-btn {{
                    background: rgba(99, 102, 241, 0.08);
                    border: 1px solid rgba(99, 102, 241, 0.2);
                    cursor: pointer;
                    padding: 4px;
                    color: #6366f1;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border-radius: 4px;
                    transition: all 0.2s;
                    width: 20px;
                    height: 20px;
                    min-width: 20px;
                    min-height: 20px;
                    user-select: none;
                    -webkit-user-select: none;
                    -moz-user-select: none;
                    -ms-user-select: none;
                }}
                .test-details-copy-btn:hover {{
                    background: rgba(99, 102, 241, 0.15);
                    border-color: rgba(99, 102, 241, 0.4);
                    color: #4f46e5;
                }}
                .test-details-copy-btn:active {{
                    transform: scale(0.85);
                    background: rgba(34, 197, 94, 0.2);
                    border-color: rgba(34, 197, 94, 0.5);
                    color: #22c55e;
                }}
                .test-details-copy-btn.copied {{
                    background: rgba(34, 197, 94, 0.25) !important;
                    border-color: rgba(34, 197, 94, 0.6) !important;
                    color: #16a34a !important;
                    transform: scale(1.1);
                    animation: copySuccess 0.4s ease-out;
                }}
                .test-details-copy-btn svg {{
                    width: 12px;
                    height: 12px;
                    fill: none;
                    stroke: currentColor;
                    stroke-width: 2.5;
                    stroke-linecap: round;
                    stroke-linejoin: round;
                }}
                .test-details-content code {{
                    word-break: break-all;
                    overflow-wrap: break-word;
                    white-space: pre-wrap;
                    max-width: 100%;
                    display: inline-block;
                }}
                .test-details-content div {{
                    max-width: 100%;
                    overflow-wrap: break-word;
                    word-break: break-word;
                }}
                
                /* Footer */
                .footer {{ background: #f8f9fa; padding: 20px; text-align: center; font-size: 12px; color: #999; border-top: 1px solid #eee; }}
    """
