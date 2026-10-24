document.addEventListener('DOMContentLoaded', () => {
    if (document.documentElement.classList.contains('is-tizen-tv') || document.documentElement.classList.contains('is-mobile-view') || (window.innerWidth > 0 && window.innerWidth <= 768)) return;
    const section = document.getElementById('vodCommentsSection');
    if (!section) return;

    const vodId = section.dataset.vodId;
    const previewBox = document.getElementById('vodCommentsPreview');
    const expandedBox = document.getElementById('vodCommentsExpanded') || section.querySelector('.vod-comments-expanded');
    const closeBtn = document.getElementById('vodCommentsClose');
    const countSpan = document.getElementById('vodCommentsCount');
    const countExpandedSpan = document.getElementById('vodCommentsCountExpanded');
    const topPreview = document.getElementById('vodCommentsTopPreview');
    const listContainer = document.getElementById('vodCommentsList');

    const commentInput = document.getElementById('vodCommentInput');
    const submitBtn = document.getElementById('vodCommentSubmit');
    const cancelBtn = document.getElementById('vodCommentCancel');
    const actionsBox = document.querySelector('.vod-comment-actions');

    let commentsData = [];

    function formatTime(ts) {
        const d = new Date(ts * 1000);
        return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
    }

    function renderComments() {
        if (countSpan) countSpan.textContent = commentsData.length;
        if (countExpandedSpan) countExpandedSpan.textContent = commentsData.length;

        if (topPreview) {
            if (commentsData.length > 0) {
                topPreview.textContent = `${commentsData[0].username}: ${commentsData[0].comment}`;
            } else {
                topPreview.textContent = 'Schreibe den ersten Kommentar...';
            }
        }

        listContainer.innerHTML = '';
        if (commentsData.length === 0) {
            listContainer.innerHTML = '<div style="padding: 20px 0; color: #a1a1aa; text-align: center;">Noch keine Kommentare.</div>';
            return;
        }

        commentsData.forEach(c => {
            const item = document.createElement('div');
            item.className = 'vod-comment-item';
            
            const avatar = document.createElement('div');
            avatar.className = 'vod-comment-avatar';
            avatar.textContent = c.username ? c.username.charAt(0).toUpperCase() : '?';
            
            const body = document.createElement('div');
            body.className = 'vod-comment-body';
            
            const meta = document.createElement('div');
            meta.className = 'vod-comment-meta';
            meta.innerHTML = `<span class="vod-comment-author">${c.username}</span><span class="vod-comment-time">${formatTime(c.timestamp)}</span>`;
            
            const text = document.createElement('div');
            text.className = `vod-comment-text ${c.is_deleted ? 'deleted' : ''}`;
            text.textContent = c.comment;

            body.appendChild(meta);
            body.appendChild(text);
            item.appendChild(avatar);
            item.appendChild(body);
            listContainer.appendChild(item);
        });
    }

    async function loadComments() {
        try {
            const res = await fetch(`/api/vod_comments?vod_id=${encodeURIComponent(vodId)}`);
            if (res.ok) {
                commentsData = await res.json();
                renderComments();
            } else if (topPreview) {
                topPreview.textContent = 'Fehler beim Laden';
            }
        } catch (e) {
            if (topPreview) topPreview.textContent = 'Fehler beim Laden';
        }
    }

    async function postComment() {
        const text = commentInput.value.trim();
        if (!text) return;
        submitBtn.disabled = true;

        try {
            const res = await fetch('/api/vod_comments', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': window.__NACHTSILO_CSRF || '' 
                },
                body: JSON.stringify({
                    vod_id: vodId,
                    comment: text
                })
            });

            if (res.ok) {
                const newComment = await res.json();
                commentsData.unshift(newComment); // Add to top
                renderComments();
                commentInput.value = '';
                commentInput.blur();
                if (actionsBox) actionsBox.classList.remove('is-visible');
            } else {
                alert('Fehler beim Senden des Kommentars');
            }
        } catch (e) {
            alert('Fehler beim Senden des Kommentars');
        } finally {
            submitBtn.disabled = false;
        }
    }

    const alwaysOpenMobile = section.classList.contains('mobile-comments')
        || section.getAttribute('data-mobile-always-open') === '1';

    function openComments() {
        section.classList.add('is-open');
        if (expandedBox) expandedBox.style.display = 'flex';
        if (!alwaysOpenMobile && previewBox) {
            previewBox.style.display = 'none';
            previewBox.hidden = true;
        }
    }

    function closeComments() {
        if (alwaysOpenMobile) return;
        section.classList.remove('is-open');
        if (expandedBox) expandedBox.style.display = 'none';
        if (previewBox) {
            previewBox.style.display = 'block';
            previewBox.hidden = false;
        }
    }

    if (alwaysOpenMobile) {
        openComments();
    } else if (previewBox) {
        previewBox.addEventListener('click', openComments);
    }

    if (closeBtn) {
        closeBtn.addEventListener('click', closeComments);
    }

    commentInput.addEventListener('focus', () => {
        if (actionsBox) actionsBox.classList.add('is-visible');
    });

    commentInput.addEventListener('input', () => {
        submitBtn.disabled = commentInput.value.trim().length === 0;
    });

    commentInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (!submitBtn.disabled) {
                postComment();
            }
        }
    });

    if (cancelBtn) {
        cancelBtn.addEventListener('click', () => {
            commentInput.value = '';
            if (actionsBox) if (actionsBox) actionsBox.classList.remove('is-visible');
        });
    }

    submitBtn.addEventListener('click', postComment);

    // Initial load
    loadComments();
});
