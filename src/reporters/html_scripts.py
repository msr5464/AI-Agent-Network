"""
JavaScript code for HTML report generation.
Extracted from report_generator.py for better maintainability.
"""


def get_html_scripts(dashboard_base_url: str, project_name: str, job_name: str, jira_base_url: str = '') -> str:
    """
    Generate JavaScript code for the HTML report.

    Args:
        dashboard_base_url: Base URL for the dashboard (e.g., "https://dashboard.qa.example.com")
        project_name: Project name for building URLs
        job_name: Job name for building URLs
        jira_base_url: Base URL for Jira (e.g., "https://jira.example.com") for known-failure ticket links

    Returns:
        JavaScript code as a string
    """
    # Escape single quotes in the values to prevent JavaScript errors
    dashboard_base_url_escaped = dashboard_base_url.replace("'", "\\'")
    project_name_escaped = project_name.replace("'", "\\'")
    job_name_escaped = job_name.replace("'", "\\'")
    jira_base_url_escaped = (jira_base_url or '').rstrip('/').replace("'", "\\'")

    # Use triple quotes with string concatenation to avoid issues with JavaScript braces
    return (
        """            // Configuration from server
            const DASHBOARD_BASE_URL = '""" + dashboard_base_url_escaped + """';
            const PROJECT_NAME = '""" + project_name_escaped + """';
            const JOB_NAME = '""" + job_name_escaped + """';
            const JIRA_BASE_URL = '""" + jira_base_url_escaped + """';
            const newlineChar = '\\n';
            
            // Handle expand icon click to toggle details and update animation
            document.addEventListener('click', function(event) {
                if (event.target.classList.contains('test-expand-icon')) {
                    event.preventDefault();
                    event.stopPropagation();
                    const icon = event.target;
                    const chipContainer = icon.closest('.test-chip-with-details');
                    if (chipContainer) {
                        const details = chipContainer.querySelector('.test-details-expandable');
                        if (details) {
                            const wasOpen = details.open;
                            details.open = !wasOpen;
                            // Update icon class for animation
                            if (details.open) {
                                icon.classList.add('expanded');
                            } else {
                                icon.classList.remove('expanded');
                            }
                        }
                    }
                }
            });
            
            // Also handle clicks on the details summary to sync icon animation
            document.addEventListener('click', function(event) {
                const summary = event.target.closest('.test-details-summary');
                if (summary) {
                    const details = summary.closest('.test-details-expandable');
                    if (details) {
                        const chipContainer = details.closest('.test-chip-with-details');
                        if (chipContainer) {
                            const icon = chipContainer.querySelector('.test-expand-icon');
                            if (icon) {
                                // Small delay to sync with details open state
                                setTimeout(function() {
                                    if (details.open) {
                                        icon.classList.add('expanded');
                                    } else {
                                        icon.classList.remove('expanded');
                                    }
                                }, 10);
                            }
                        }
                    }
                }
            });
            
            // Watch for details open/close changes to sync icon animation
            document.querySelectorAll('.test-details-expandable').forEach(function(details) {
                details.addEventListener('toggle', function() {
                    const chipContainer = details.closest('.test-chip-with-details');
                    if (chipContainer) {
                        const icon = chipContainer.querySelector('.test-expand-icon');
                        if (icon) {
                            if (details.open) {
                                icon.classList.add('expanded');
                            } else {
                                icon.classList.remove('expanded');
                            }
                        }
                    }
                });
            });
            
            // Close test details expandable section
            function closeTestDetailsExpandable(detailsId) {
                const details = document.getElementById(detailsId);
                if (details) {
                    details.open = false;
                    // Sync icon state
                    const chipContainer = details.closest('.test-chip-with-details');
                    if (chipContainer) {
                        const icon = chipContainer.querySelector('.test-expand-icon');
                        if (icon) {
                            icon.classList.remove('expanded');
                        }
                    }
                }
            }
            
            // Dynamic tooltip positioning to prevent clipping
            function setupTooltips() {
                document.querySelectorAll('.root-cause-copy-btn[title], .root-cause-link-btn[title]').forEach(function(btn) {
                    let tooltipEl = null;
                    let arrowEl = null;
                    
                    btn.addEventListener('mouseenter', function(e) {
                        const tooltipText = this.getAttribute('title');
                        if (!tooltipText) return;
                        
                        const rect = this.getBoundingClientRect();
                        
                        // Create tooltip element
                        tooltipEl = document.createElement('div');
                        tooltipEl.className = 'dynamic-tooltip';
                        tooltipEl.textContent = tooltipText;
                        tooltipEl.style.cssText = `
                            position: fixed;
                            top: ${rect.top - 35}px;
                            left: ${rect.left + (rect.width / 2)}px;
                            transform: translateX(-50%);
                            padding: 6px 10px;
                            background: #1f2933;
                            color: #fff;
                            font-size: 11px;
                            white-space: nowrap;
                            border-radius: 4px;
                            pointer-events: none;
                            z-index: 999999;
                            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
                        `;
                        document.body.appendChild(tooltipEl);
                        
                        // Create arrow
                        arrowEl = document.createElement('div');
                        arrowEl.className = 'dynamic-tooltip-arrow';
                        arrowEl.style.cssText = `
                            position: fixed;
                            top: ${rect.top - 7}px;
                            left: ${rect.left + (rect.width / 2)}px;
                            transform: translateX(-50%);
                            border: 5px solid transparent;
                            border-top-color: #1f2933;
                            pointer-events: none;
                            z-index: 999998;
                        `;
                        document.body.appendChild(arrowEl);
                    });
                    
                    btn.addEventListener('mouseleave', function() {
                        if (tooltipEl) {
                            tooltipEl.remove();
                            tooltipEl = null;
                        }
                        if (arrowEl) {
                            arrowEl.remove();
                            arrowEl = null;
                        }
                    });
                    
                    btn.addEventListener('mousemove', function(e) {
                        if (tooltipEl && arrowEl) {
                            const rect = this.getBoundingClientRect();
                            tooltipEl.style.top = (rect.top - 35) + 'px';
                            tooltipEl.style.left = (rect.left + (rect.width / 2)) + 'px';
                            arrowEl.style.top = (rect.top - 7) + 'px';
                            arrowEl.style.left = (rect.left + (rect.width / 2)) + 'px';
                        }
                    });
                });
            }
            
            // Initialize tooltips
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', setupTooltips);
            } else {
                setupTooltips();
            }
            
            // Toggle test details expandable section
            function toggleTestDetails(detailsId) {
                const details = document.getElementById(detailsId);
                if (details) {
                    details.open = !details.open;
                    // Sync icon state
                    const chipContainer = details.closest('.test-chip-with-details');
                    if (chipContainer) {
                        const icon = chipContainer.querySelector('.test-expand-icon');
                        if (icon) {
                            if (details.open) {
                                icon.classList.add('expanded');
                            } else {
                                icon.classList.remove('expanded');
                            }
                        }
                    }
                }
            }
            
            // Donut chart tooltip functions - create tooltip dynamically
            let donutTooltip = null;
            
            function createDonutTooltip() {
                if (donutTooltip) return donutTooltip;
                
                donutTooltip = document.createElement('div');
                donutTooltip.id = 'donut-tooltip';
                donutTooltip.style.cssText = 'position: fixed; display: none; background: #1f2933; color: #fff; padding: 10px 14px; border-radius: 6px; font-size: 12px; pointer-events: none; z-index: 1000000; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4); white-space: nowrap;';
                
                const iconSpan = document.createElement('span');
                iconSpan.id = 'donut-tooltip-icon';
                
                const labelSpan = document.createElement('span');
                labelSpan.id = 'donut-tooltip-label';
                
                const headerDiv = document.createElement('div');
                headerDiv.style.cssText = 'font-weight: 600; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;';
                headerDiv.appendChild(iconSpan);
                headerDiv.appendChild(labelSpan);
                
                const countDiv = document.createElement('div');
                countDiv.style.cssText = 'font-size: 11px; color: #d1d5db;';
                countDiv.innerHTML = 'Count: <span id="donut-tooltip-count" style="font-weight: 600; color: #fff;"></span>';
                
                const percentageDiv = document.createElement('div');
                percentageDiv.style.cssText = 'font-size: 11px; color: #d1d5db;';
                percentageDiv.innerHTML = 'Percentage: <span id="donut-tooltip-percentage" style="font-weight: 600; color: #fff;"></span>';
                
                donutTooltip.appendChild(headerDiv);
                donutTooltip.appendChild(countDiv);
                donutTooltip.appendChild(percentageDiv);
                
                document.body.appendChild(donutTooltip);
                return donutTooltip;
            }
            
            function showDonutTooltip(event, segmentId, label, icon, count, percentage, color) {
                if (!donutTooltip) {
                    donutTooltip = createDonutTooltip();
                }
                
                // Update tooltip content
                const iconEl = document.getElementById('donut-tooltip-icon');
                const labelEl = document.getElementById('donut-tooltip-label');
                const countEl = document.getElementById('donut-tooltip-count');
                const percentageEl = document.getElementById('donut-tooltip-percentage');
                
                if (iconEl) iconEl.textContent = icon;
                if (labelEl) labelEl.textContent = label;
                if (countEl) countEl.textContent = count;
                if (percentageEl) percentageEl.textContent = percentage.toFixed(1) + '%';
                
                // Position tooltip near mouse cursor
                const tooltipPadding = 15;
                let tooltipX = event.clientX + tooltipPadding;
                let tooltipY = event.clientY - tooltipPadding;
                
                // Ensure tooltip stays within viewport
                donutTooltip.style.display = 'block';
                const tooltipRect = donutTooltip.getBoundingClientRect();
                const viewportWidth = window.innerWidth;
                const viewportHeight = window.innerHeight;
                
                if (tooltipX + tooltipRect.width > viewportWidth) {
                    tooltipX = event.clientX - tooltipRect.width - tooltipPadding;
                }
                if (tooltipY + tooltipRect.height > viewportHeight) {
                    tooltipY = event.clientY - tooltipRect.height - tooltipPadding;
                }
                if (tooltipX < 0) tooltipX = tooltipPadding;
                if (tooltipY < 0) tooltipY = tooltipPadding;
                
                donutTooltip.style.left = tooltipX + 'px';
                donutTooltip.style.top = tooltipY + 'px';
                
                // Highlight the segment
                const segment = document.getElementById(segmentId);
                if (segment) {
                    segment.style.opacity = '0.8';
                    segment.style.filter = 'brightness(1.1)';
                }
            }
            
            function hideDonutTooltip(segmentId) {
                if (donutTooltip) {
                    donutTooltip.style.display = 'none';
                }
                
                // Reset segment highlight
                const segment = document.getElementById(segmentId);
                if (segment) {
                    segment.style.opacity = '1';
                    segment.style.filter = 'none';
                }
            }
            
            function updateDonutTooltipPosition(event) {
                if (!donutTooltip || donutTooltip.style.display === 'none') return;
                
                const tooltipPadding = 15;
                let tooltipX = event.clientX + tooltipPadding;
                let tooltipY = event.clientY - tooltipPadding;
                
                // Ensure tooltip stays within viewport
                const tooltipRect = donutTooltip.getBoundingClientRect();
                const viewportWidth = window.innerWidth;
                const viewportHeight = window.innerHeight;
                
                if (tooltipX + tooltipRect.width > viewportWidth) {
                    tooltipX = event.clientX - tooltipRect.width - tooltipPadding;
                }
                if (tooltipY + tooltipRect.height > viewportHeight) {
                    tooltipY = event.clientY - tooltipRect.height - tooltipPadding;
                }
                if (tooltipX < 0) tooltipX = tooltipPadding;
                if (tooltipY < 0) tooltipY = tooltipPadding;
                
                donutTooltip.style.left = tooltipX + 'px';
                donutTooltip.style.top = tooltipY + 'px';
            }
            
            // Copy test name to clipboard
            function copyTestName(testName, buttonElement, event) {
                // Prevent event propagation to avoid triggering link navigation
                if (event) {
                    event.stopPropagation();
                    event.preventDefault();
                }
                
                // Add visual feedback function
                function showCopySuccess() {
                    if (buttonElement) {
                        // Remove any existing copied class
                        buttonElement.classList.remove('copied');
                        // Force reflow to restart animation
                        void buttonElement.offsetWidth;
                        // Add copied class to trigger animation
                        buttonElement.classList.add('copied');
                        // Remove class after animation completes
                        setTimeout(function() {
                            buttonElement.classList.remove('copied');
                        }, 400);
                    }
                }
                
                // Copy to clipboard
                navigator.clipboard.writeText(testName).then(function() {
                    showCopySuccess();
                }).catch(function(err) {
                    // Fallback for older browsers
                    const textArea = document.createElement('textarea');
                    textArea.value = testName;
                    textArea.style.position = 'fixed';
                    textArea.style.opacity = '0';
                    document.body.appendChild(textArea);
                    textArea.select();
                    try {
                        document.execCommand('copy');
                        showCopySuccess();
                    } catch (err) {
                        console.error('Failed to copy:', err);
                    }
                    document.body.removeChild(textArea);
                });
            }
            
            // Track currently active dot for visual indication
            let activeDotId = null;
            let activeDetailsRowId = null;
            
            function highlightActiveDot(dotId) {
                // Remove highlighting from previously active dot
                if (activeDotId && activeDotId !== dotId) {
                    const prevDot = document.getElementById(activeDotId);
                    if (prevDot) {
                        prevDot.style.border = '';
                        prevDot.style.boxShadow = '';
                        prevDot.style.transform = 'scale(1)';
                    }
                }
                
                // Highlight the new active dot
                const dot = document.getElementById(dotId);
                if (dot) {
                    dot.style.border = '2px solid #007bff';
                    dot.style.boxShadow = '0 0 8px rgba(0, 123, 255, 0.5)';
                    dot.style.transform = 'scale(1.3)';
                    activeDotId = dotId;
                }
            }
            
            function removeDotHighlight() {
                if (activeDotId) {
                    const dot = document.getElementById(activeDotId);
                    if (dot) {
                        dot.style.border = '';
                        dot.style.boxShadow = '';
                        dot.style.transform = 'scale(1)';
                    }
                    activeDotId = null;
                }
            }
            
            function closeExecutionDetails(detailsRowId) {
                const detailsRow = document.getElementById(detailsRowId);
                if (detailsRow) {
                    detailsRow.style.display = 'none';
                    removeDotHighlight();
                    activeDetailsRowId = null;
                }
            }
            
            function toggleExecutionDetails(dotId, testName, executionIndex) {
                console.log('toggleExecutionDetails called:', dotId, testName, executionIndex);
                // Get the dot element
                const dot = document.getElementById(dotId);
                if (!dot) {
                    console.error('Dot not found:', dotId);
                    return;
                }
                
                // Check if this dot has execution data (not padded)
                const isPadded = dot.getAttribute('data-is-padded') === 'true';
                if (isPadded) {
                    console.log('Dot is padded, skipping');
                    return; // Don't show details for padded entries
                }
                
                // Get execution details from data attributes
                const execId = dot.getAttribute('data-execution-id') || 'N/A';
                const execDate = dot.getAttribute('data-execution-date') || 'N/A';
                const execBuild = dot.getAttribute('data-execution-build') || 'N/A';
                const execError = dot.getAttribute('data-execution-error') || '';
                const execStatus = dot.getAttribute('data-execution-status') || 'N/A';
                const historyStatus = dot.getAttribute('data-history-status') || ''; // 'pass' or 'fail'
                
                // Determine if this is a pass, fail, or known failure
                // Priority: 1. history-status attribute, 2. execStatus, 3. dot color
                const isKnownFailure = historyStatus === 'known_failure';
                const isFailure = historyStatus === 'fail' || 
                                 (execStatus.toUpperCase().includes('FAIL') || 
                                  execStatus.toUpperCase().includes('ERROR')) && !isKnownFailure;
                const isPass = (historyStatus === 'pass' || (!isFailure && historyStatus !== 'fail' && !isKnownFailure));
                
                // Find the details row - it's the next sibling of the parent row
                const parentRow = dot.closest('tr');
                if (!parentRow) {
                    console.error('Parent row not found');
                    return;
                }
                
                const actualDetailsRow = parentRow.nextElementSibling;
                if (!actualDetailsRow || !actualDetailsRow.classList.contains('execution-details-row')) {
                    console.error('Details row not found. Next sibling:', actualDetailsRow);
                    return; // Details row not found
                }
                
                const detailsRowId = actualDetailsRow.id;
                
                // Get content div - it's the div with id ending in _content
                const contentDiv = document.getElementById(detailsRowId + '_content');
                if (!contentDiv) {
                    console.error('Content div not found for:', detailsRowId);
                    return;
                }
                
                // Toggle visibility
                const isVisible = actualDetailsRow.style.display !== 'none';
                
                if (isVisible) {
                    // Check if clicking the same dot - collapse
                    const currentIndex = contentDiv.getAttribute('data-current-index');
                    if (currentIndex === String(executionIndex)) {
                        closeExecutionDetails(detailsRowId);
                        return;
                    }
                }
                
                // Show details - determine status based on dot color and execStatus
                let statusColor, statusText;
                if (isKnownFailure) {
                    statusColor = '#ffc107';
                    statusText = '⚠️ FAILED due to known issue, but marked as PASSED!';
                } else if (isFailure) {
                    statusColor = '#dc3545';
                    statusText = '❌ Failed';
                } else {
                    statusColor = '#28a745';
                    statusText = '✅ Passed';
                }
                
                // Build error message row if it's a failure OR known failure and has error message
                // Known failures also have failureReason data that should be displayed
                // Trim leading/trailing whitespace and normalize excessive whitespace
                let cleanedError = execError || '';
                let errorMessageRow = '';
                if ((isFailure || isKnownFailure) && cleanedError && cleanedError !== 'N/A' && cleanedError.trim() !== '') {
                    try {
                        // Decode HTML entities first to work with actual text
                        const tempDiv = document.createElement('div');
                        tempDiv.innerHTML = cleanedError;
                        let decodedError = tempDiv.textContent || tempDiv.innerText || cleanedError;
                        
                        // Remove leading whitespace from each line
                        // Note: "Results Url:" lines are already removed when fetching from DB
                        decodedError = decodedError.split(newlineChar).map(function(line) {
                            return line.replace(/^\\s+/, '');
                        }).join(newlineChar);
                        // Trim overall leading/trailing whitespace
                        decodedError = decodedError.trim();
                        
                        // Re-escape for HTML display
                        cleanedError = decodedError.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
                        
                        // Use different border color for known failures (yellow) vs actual failures (red)
                        const borderColor = isKnownFailure ? '#ffc107' : '#dc3545';
                        errorMessageRow = '<tr>' +
                            '<td style="padding: 4px; font-weight: 600; color: #6c757d; vertical-align: top; text-align: left;">Error Message:</td>' +
                            '<td style="padding: 4px; text-align: left;">' +
                            '<div style="background-color: #fff; padding: 5px 5px 5px 5px; border-radius: 3px; border-left: 2px solid ' + borderColor + '; font-family: monospace; font-size: 11px; max-height: 240px; overflow-y: auto; white-space: pre-wrap; word-wrap: break-word; text-align: left; margin: 0;">' +
                            cleanedError +
                            '</div>' +
                            '</td>' +
                            '</tr>';
                    } catch (e) {
                        console.error('Error processing error message:', e);
                        // Fallback: use original error message
                        const borderColor = isKnownFailure ? '#ffc107' : '#dc3545';
                        errorMessageRow = '<tr>' +
                            '<td style="padding: 4px; font-weight: 600; color: #6c757d; vertical-align: top; text-align: left;">Error Message:</td>' +
                            '<td style="padding: 4px; text-align: left;">' +
                            '<div style="background-color: #fff; padding: 5px 5px 5px 5px; border-radius: 3px; border-left: 2px solid ' + borderColor + '; font-family: monospace; font-size: 11px; max-height: 240px; overflow-y: auto; white-space: pre-wrap; word-wrap: break-word; text-align: left; margin: 0;">' +
                            (execError || 'Error message unavailable') +
                            '</div>' +
                            '</td>' +
                            '</tr>';
                    }
                }
                
                // Build table rows - only show relevant information (increased by 20% from reduced size)
                let tableRows = `
                    <tr>
                        <td style="padding: 4px; width: 130px; font-weight: 600; color: #6c757d; font-size: 12px; text-align: left;">Status:</td>
                        <td style="padding: 4px; font-size: 12px; text-align: left;"><span style="color: ${statusColor}; font-weight: 600;">${statusText}</span></td>
                    </tr>
                `;
                
                // Only show date if available
                if (execDate && execDate !== 'N/A' && execDate.trim() !== '') {
                    tableRows += `
                        <tr>
                            <td style="padding: 4px; font-weight: 600; color: #6c757d; font-size: 12px; text-align: left;">Execution Date:</td>
                            <td style="padding: 4px; font-size: 12px; text-align: left;">${execDate}</td>
                        </tr>
                    `;
                }
                
                // Only show build URL if available
                const execUrl = dot.getAttribute('data-execution-url') || '';
                
                if (execUrl && execUrl.trim() !== '') {
                    const buildUrl = execUrl;
                    tableRows += `
                        <tr>
                            <td style="padding: 4px; font-weight: 600; color: #6c757d; font-size: 12px; text-align: left;">Build Url:</td>
                            <td style="padding: 4px; font-size: 12px; text-align: left;">
                                <a href="${buildUrl}" target="_blank" style="color: #007bff; text-decoration: none;">${execBuild}</a>
                            </td>
                        </tr>
                    `;
                }
                
                // Only show execution ID if available and not None/null
                if (execId && execId !== 'N/A' && execId !== 'None' && execId !== 'null' && execId.trim() !== '') {
                    tableRows += `
                        <tr>
                            <td style="padding: 4px; font-weight: 600; color: #6c757d; font-size: 12px; text-align: left;">Execution ID:</td>
                            <td style="padding: 4px; font-size: 12px; text-align: left;">${execId}</td>
                        </tr>
                    `;
                }
                
                // Show known failure info if applicable (JIRA_BASE_URL from Config, injected by report generator)
                const knownFailureTicket = dot.getAttribute('data-known-failure') || '';
                if (isKnownFailure && knownFailureTicket && knownFailureTicket.trim() !== '') {
                    const jiraBase = (JIRA_BASE_URL || '').trim();
                    const jiraUrl = jiraBase ? (jiraBase + '/browse/' + knownFailureTicket) : '';
                    const ticketHtml = jiraUrl
                        ? `<a href="${jiraUrl}" target="_blank" style="color: #ff9800; text-decoration: none; font-weight: 600;">${knownFailureTicket}</a>`
                        : `<span style="color: #ff9800; font-weight: 600;">${knownFailureTicket}</span>`;
                    tableRows += `
                        <tr>
                            <td style="padding: 4px; font-weight: 600; color: #6c757d; font-size: 12px; text-align: left;">Known Issue Ticket:</td>
                            <td style="padding: 4px; font-size: 12px; text-align: left;">
                                ${ticketHtml}
                                <span style="color: #6c757d; font-size: 11px; margin-left: 8px;">(Test failed but marked as passed due to this known issue)</span>
                            </td>
                        </tr>
                    `;
                }
                
                // Show error message for failures and known failures (both have failureReason data)
                if (errorMessageRow) {
                    tableRows += errorMessageRow;
                }
                
                contentDiv.innerHTML = `
                    <div style="margin-bottom: 7px; display: flex; align-items: center; gap: 5px; text-align: left;">
                        <strong style="color: #495057; font-size: 12px;">Execution #${executionIndex + 1}</strong>
                        <span style="color: #999; font-size: 10px;">(${executionIndex === 0 ? 'Oldest' : executionIndex === 9 ? 'Newest' : 'Middle'})</span>
                    </div>
                    <table style="width: 100%; border-collapse: collapse; font-size: 12px; text-align: left; margin: 0; padding: 0;">
                        ${tableRows}
                    </table>
                `;
                
                contentDiv.setAttribute('data-current-index', executionIndex);
                actualDetailsRow.style.display = 'table-row';
                activeDetailsRowId = detailsRowId;
                
                // Highlight the active dot
                highlightActiveDot(dotId);
                
                // Scroll to details if needed
                actualDetailsRow.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
            
            // Add click handlers and hover effects to dots
            document.addEventListener('DOMContentLoaded', function() {
                const dots = document.querySelectorAll('.history-dot');
                dots.forEach(dot => {
                    const isPadded = dot.getAttribute('data-is-padded') === 'true';
                    const dotId = dot.id;
                    const testName = dot.getAttribute('data-test-name') || '';
                    const executionIndex = parseInt(dot.getAttribute('data-execution-index') || '0');
                    
                    // Add click handler
                    dot.addEventListener('click', function(e) {
                        e.preventDefault();
                        e.stopPropagation();
                        toggleExecutionDetails(dotId, testName, executionIndex);
                    });
                    
                    // Add hover effects for non-padded dots
                    if (!isPadded) {
                        dot.addEventListener('mouseenter', function() {
                            // Only apply hover effect if not currently active
                            if (this.id !== activeDotId) {
                                this.style.opacity = '0.7';
                                this.style.transform = 'scale(1.2)';
                            }
                        });
                        dot.addEventListener('mouseleave', function() {
                            // Only reset if not currently active
                            if (this.id !== activeDotId) {
                                this.style.opacity = '1';
                                this.style.transform = 'scale(1)';
                            }
                        });
                    }
                });
            });
        """
    )
