import { useEffect, useMemo, useState } from "react";

const STATUS_LABEL = {
  unreviewed: "Chờ duyệt",
  supported: "Đã xác nhận",
  uncertain: "Chưa chắc chắn",
  rejected: "Loại bỏ",
};

const FILTERS = [
  ["all", "Tất cả"],
  ["unreviewed", "Chờ duyệt"],
  ["supported", "Đã xác nhận"],
  ["uncertain", "Chưa chắc"],
  ["rejected", "Đã loại"],
];

const STEP_LABEL = {
  article_research: "đọc và tóm tắt tin",
  script_review: "viết và rà soát chữ",
  image_search: "tìm ảnh theo tin",
  audio_search: "chọn nhạc NCS ngẫu nhiên",
  image_and_audio: "lấy ảnh nguồn và nhạc NCS",
  video_render: "ghép ảnh, chữ và âm thanh thành video",
  complete: "dựng video xong",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Không thể kết nối tới backend.");
  return payload;
}

function toForm(story) {
  return {
    competition_confirmed: Boolean(story.competition_confirmed),
    review_status: story.review_status || "unreviewed",
    summary_vi: story.summary_vi || "",
    evidence_notes: story.evidence_notes || "",
  };
}

function hasCurrentVideoText(story) {
  const lines = story?.video_lines;
  return Array.isArray(lines)
    && lines.length === 3
    && lines.every((line) => typeof line === "string" && line.trim() && line.length <= 48)
    && (story.generated_script || "").trim() === lines.join("\n").trim();
}

function App() {
  const [items, setItems] = useState([]);
  const [archiveItems, setArchiveItems] = useState([]);
  const [page, setPage] = useState("newsroom");
  const [counts, setCounts] = useState({});
  const [filter, setFilter] = useState("unreviewed");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const [form, setForm] = useState(null);
  const [savedSnapshot, setSavedSnapshot] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [draftResult, setDraftResult] = useState(null);
  const [agentRun, setAgentRun] = useState(null);
  const [scriptText, setScriptText] = useState("");
  const [youtubeStatus, setYoutubeStatus] = useState({ configured: false, connected: false, loading: true });

  async function loadStories(keepSelection = true, preferredId = null) {
    setError("");
    try {
      const result = await api("/api/stories");
      setItems(result.stories);
      setCounts(result.counts);
      const nextId = preferredId && result.stories.some((item) => item.story_id === preferredId)
        ? preferredId
        : keepSelection && result.stories.some((item) => item.story_id === selectedId)
          ? selectedId
          : result.stories[0]?.story_id || null;
      setSelectedId(nextId);
      const selected = result.stories.find((item) => item.story_id === nextId);
      if (selected) {
        const nextForm = toForm(selected);
        setForm(nextForm);
        setSavedSnapshot(JSON.stringify(nextForm));
        setScriptText(hasCurrentVideoText(selected) ? selected.generated_script : "");
      }
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    loadStories(false);
    refreshYouTubeStatus();
  }, []);

  async function refreshYouTubeStatus() {
    try {
      const status = await api("/api/youtube/status");
      setYoutubeStatus({ ...status, loading: false });
      return status;
    } catch (err) {
      setYoutubeStatus({ configured: false, connected: false, loading: false, error: err.message });
      return null;
    }
  }

  async function loadArchive() {
    setError("");
    try {
      const result = await api("/api/videos/archive");
      setArchiveItems(result.videos || []);
    } catch (err) {
      setError(err.message);
    }
  }

  const visibleItems = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("vi");
    return items.filter((item) => {
      const matchesFilter = filter === "all" || item.review_status === filter;
      const matchesQuery = !query
        || item.title.toLocaleLowerCase("vi").includes(query)
        || item.sources?.some((source) => source.source_name.toLocaleLowerCase("vi").includes(query));
      return matchesFilter && matchesQuery;
    });
  }, [items, filter, search]);

  const selected = items.find((item) => item.story_id === selectedId);
  const currentText = hasCurrentVideoText(selected);
  const approvedText = currentText && selected?.script_approved;
  const scriptChanged = Boolean(selected && scriptText.trim() !== (currentText ? selected.generated_script : "").trim());
  const isDirty = form && JSON.stringify(form) !== savedSnapshot;
  const approvedCount = (counts.supported || 0) + (counts.uncertain || 0);

  function selectStory(story) {
    if (isDirty && !window.confirm("Bạn có thay đổi chưa lưu. Bỏ thay đổi và mở story khác?")) return;
    const nextForm = toForm(story);
    setSelectedId(story.story_id);
    setForm(nextForm);
    setSavedSnapshot(JSON.stringify(nextForm));
    setScriptText(hasCurrentVideoText(story) ? story.generated_script : "");
    setMessage("");
    setDraftResult(null);
  }

  function updateField(name, value) {
    setForm((previous) => ({ ...previous, [name]: value }));
    setMessage("");
  }

  async function saveReview() {
    if (!selected || !form) return false;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api("/api/stories/" + encodeURIComponent(selected.story_id), {
        method: "PATCH",
        body: JSON.stringify(form),
      });
      setItems((previous) => previous.map((item) =>
        item.story_id === selected.story_id ? result.story : item
      ));
      setCounts(result.counts);
      const nextForm = toForm(result.story);
      setForm(nextForm);
      setSavedSnapshot(JSON.stringify(nextForm));
      setScriptText(result.story.generated_script || "");
      setMessage("Đã lưu quyết định duyệt.");
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function generateDrafts() {
    if (isDirty) {
      const shouldSave = window.confirm("Lưu thay đổi hiện tại trước khi tạo kịch bản?");
      if (!shouldSave) return;
      const saved = await saveReview();
      if (!saved) return;
    }
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api("/api/drafts", { method: "POST", body: "{}" });
      setDraftResult(result);
      setMessage("Đã tạo " + result.created + " kịch bản; bỏ qua " + result.skipped + " story chưa đủ điều kiện.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function runNewsroomAgent() {
    if (isDirty) {
      const saved = await saveReview();
      if (!saved) return;
    }
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api("/api/agent/run", {
        method: "POST",
        body: JSON.stringify({ limit: 5 }),
      });
      const finishedStory = result.processed.find((item) => item.status === "complete")?.story_id;
      await loadStories(false, finishedStory);
      setFilter("all");
      setAgentRun(result);
      const done = result.completed_videos ?? result.processed.filter((item) => item.status === "complete").length;
      const pending = result.processed.filter((item) => item.status === "needs_review").length;
      const skipped = result.processed.filter((item) => item.status === "skipped").length;
      const failed = result.processed.filter((item) => item.status === "error").length;
      const target = result.target_videos ?? 5;
      setMessage(result.target_met
        ? `Đã lấy ${result.stories_seen} story từ RSS và hoàn tất đủ ${done}/${target} video.`
        : `Đã lấy ${result.stories_seen} story từ RSS nhưng chỉ hoàn tất ${done}/${target} video; còn thiếu ${result.shortfall ?? target - done}. ${pending} cần rà soát, ${skipped} bỏ qua, ${failed} lỗi.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function generateSelectedScript() {
    if (!selected) return;
    if (isDirty) {
      const saved = await saveReview();
      if (!saved) return;
    }
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api(`/api/stories/${encodeURIComponent(selected.story_id)}/generate-script`, {
        method: "POST", body: "{}",
      });
      setItems((previous) => previous.map((item) => item.story_id === selected.story_id ? result.story : item));
      setScriptText(result.story.generated_script || "");
      setMessage(result.story.script_approved
        ? "AI đã rà soát ba dòng chữ. Có thể tạo video ảnh + nhạc."
        : "AI yêu cầu rà soát thêm. Hãy xem ghi chú, chỉnh ba dòng rồi gửi AI rà soát lại.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function reviewScript(editorApproved = false) {
    if (!selected || scriptText.trim().length < 3) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api(`/api/stories/${encodeURIComponent(selected.story_id)}/script`, {
        method: "PATCH", body: JSON.stringify({ script_text: scriptText, editor_approved: editorApproved }),
      });
      setItems((previous) => previous.map((item) => item.story_id === selected.story_id ? result.story : item));
      setMessage(result.story.script_approved
        ? editorApproved ? "Đã xác nhận chữ. Có thể tạo video để xem trước." : "AI đã rà soát ba dòng chữ. Có thể tạo video ảnh + nhạc."
        : "AI yêu cầu rà soát thêm. Hãy xem ghi chú, chỉnh ba dòng rồi gửi AI rà soát lại.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function renderSelectedVideo() {
    if (!selected) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api(`/api/stories/${encodeURIComponent(selected.story_id)}/render-video`, {
        method: "POST", body: "{}",
      });
      setItems((previous) => previous.map((item) => item.story_id === selected.story_id ? result.story : item));
      setMessage("Đã dựng video dọc. Xem và tải bản MP4 ở bên dưới.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function connectYouTube() {
    const popup = window.open("/api/youtube/oauth/start", "youtube-oauth", "width=620,height=760");
    if (!popup) {
      setError("Trình duyệt đã chặn cửa sổ đăng nhập Google. Cho phép popup rồi thử lại.");
      return;
    }
    setBusy(true);
    setError("");
    setMessage("Đang chờ bạn cấp quyền upload cho kênh YouTube trong cửa sổ Google…");
    let authFailed = false;
    const handleOAuthMessage = (event) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type === "youtube-oauth-error") {
        authFailed = true;
        setError("OAuth không hoàn tất. Kiểm tra Google OAuth Client loại Desktop app và thử lại.");
        popup.close();
      }
    };
    window.addEventListener("message", handleOAuthMessage);
    const deadline = Date.now() + 5 * 60 * 1000;
    try {
      while (Date.now() < deadline) {
        await new Promise((resolve) => window.setTimeout(resolve, 1800));
        const status = await refreshYouTubeStatus();
        if (status?.connected) {
          popup.close();
          setMessage("Đã kết nối YouTube. Video đã render có thể đăng.");
          return;
        }
        if (popup.closed) {
          if (!authFailed) setError("Cửa sổ OAuth đã đóng trước khi kết nối hoàn tất.");
          return;
        }
      }
      popup.close();
      setError("Hết thời gian kết nối YouTube. Hãy thử lại.");
    } finally {
      window.removeEventListener("message", handleOAuthMessage);
      setBusy(false);
    }
  }

  async function uploadVideo(story) {
    if (!story?.video_url || !youtubeStatus.connected || (story.story_id === selectedId && isDirty)) return;
    setBusy(true);
    setError("");
    setMessage("Đang tải MP4 lên YouTube…");
    try {
      const result = await api(`/api/stories/${encodeURIComponent(story.story_id)}/youtube-upload`, {
        method: "POST", body: "{}",
      });
      setItems((previous) => previous.map((item) => item.story_id === story.story_id ? result.story : item));
      setArchiveItems((previous) => previous.map((item) => item.story_id === story.story_id ? result.story : item));
      setMessage(result.story.youtube_privacy_status === "public"
        ? "Đã đăng Short công khai lên YouTube."
        : `YouTube đã nhận video nhưng đang để ở chế độ ${result.story.youtube_privacy_status}.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function uploadSelectedVideo() {
    return uploadVideo(selected);
  }

  async function deleteArchivedVideo(story) {
    if (!window.confirm(`Xóa video và ảnh đã tải của “${story.title}” khỏi máy? Video đã đăng trên YouTube sẽ không bị xóa.`)) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api(`/api/stories/${encodeURIComponent(story.story_id)}/video`, { method: "DELETE" });
      setArchiveItems((previous) => previous.filter((item) => item.story_id !== story.story_id));
      setItems((previous) => previous.map((item) => item.story_id === story.story_id
        ? {
            ...item,
            video_path: null, video_url: null, credits_url: null, video_created_at: null,
            image_path: null, image_credit: null, image_license_url: null,
            image_page_url: null, image_title: null, image_changes: null,
          }
        : item));
      setMessage("Đã xóa video, poster, thông tin ghi công và ảnh riêng của story khỏi máy.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark">PL</div>
          <div>
            <p className="eyebrow">FOOTBALL NEWSROOM</p>
            <h1>Football AI Newsroom</h1>
          </div>
        </div>
        <div className="top-actions">
          <div className="pipeline-state"><span /> Tin → ảnh → chữ + âm thanh → video → YouTube</div>
          <button className={"button " + (page === "newsroom" ? "button-primary" : "button-secondary")} onClick={() => setPage("newsroom")}>Newsroom</button>
          <button className={"button " + (page === "archive" ? "button-primary" : "button-secondary")} onClick={() => { setPage("archive"); loadArchive(); }}>Lưu trữ video</button>
          <button
            className="button button-secondary"
            onClick={() => {
              if (page === "archive") loadArchive();
              else if (!isDirty || window.confirm("Bạn có thay đổi chưa lưu. Tải lại và bỏ thay đổi?")) loadStories();
            }}
            disabled={busy}
          >
            <span aria-hidden="true">↻</span> Tải lại
          </button>
        </div>
      </header>

      {error && <div className="notice notice-error" role="alert">{error}</div>}
      {message && <div className="notice notice-success" role="status">{message}</div>}
      {page === "archive" ? (
        <section className="archive-page">
          <div className="archive-heading">
            <div>
              <p className="eyebrow">VIDEO ĐÃ HOÀN TẤT</p>
              <h2>Lưu trữ video <span>{archiveItems.length}</span></h2>
              <p className="intro-copy">Xem trước, đăng lên YouTube Shorts hoặc xóa file khỏi máy. Video và media tự xóa sau 2 giờ.</p>
            </div>
            <div className="archive-actions">
              {!youtubeStatus.connected && youtubeStatus.configured && <button className="button button-secondary" onClick={connectYouTube} disabled={busy}>Kết nối YouTube</button>}
              {!youtubeStatus.configured && <span className="field-hint">Cần cấu hình YouTube OAuth để đăng video.</span>}
              <button className="button button-secondary" onClick={loadArchive} disabled={busy}>Làm mới danh sách</button>
            </div>
          </div>
          {archiveItems.length ? <div className="archive-grid">
            {archiveItems.map((story) => (
              <article className="archive-card" key={story.story_id}>
                <video controls playsInline preload="metadata" src={story.video_url} />
                <div className="archive-card-body">
                  <div className="archive-card-meta"><span>{formatDate(story.video_created_at)}</span><span>MP4 · 9:16</span></div>
                  <h3>{story.title}</h3>
                  {story.summary_vi && <p>{story.summary_vi}</p>}
                  <div className="archive-card-actions">
                    {story.youtube_url
                      ? <a className="button button-secondary" href={story.youtube_url} target="_blank" rel="noreferrer">Đã đăng · mở YouTube ↗</a>
                      : <button className="button button-primary" onClick={() => uploadVideo(story)} disabled={busy || !youtubeStatus.connected || (story.story_id === selectedId && isDirty)}>
                          {busy ? "Đang tải lên…" : "Đăng lên YouTube Shorts"}
                        </button>}
                    <button className="button button-danger" onClick={() => deleteArchivedVideo(story)} disabled={busy}>Xóa video</button>
                  </div>
                  {story.youtube_upload_error && <div className="ai-review-notes">Lỗi đăng gần nhất: {story.youtube_upload_error}</div>}
                  {!youtubeStatus.connected && !story.youtube_url && <small className="archive-hint">Kết nối tài khoản YouTube để bật nút đăng.</small>}
                </div>
              </article>
            ))}
          </div> : <div className="archive-empty"><strong>Chưa có video đang lưu</strong><span>Video hoàn tất sẽ xuất hiện tại đây. Video được tự động xóa sau 2 giờ.</span></div>}
        </section>
      ) : <>
      <section className="intro-row">
        <div>
          <p className="eyebrow">NGOẠI HẠNG ANH · NEWSROOM AGENT</p>
          <h2>Lấy tin, ghép ảnh và tóm tắt thành video.</h2>
          <p className="intro-copy">Lấy tin mới, tiếp tục xử lý cho đến khi đủ 5 video hoàn tất, rồi xem trước và bấm đăng. MP4 cùng media tải về tự xóa sau 2 giờ.</p>
        </div>
        <div className="top-actions">
          <button className="button button-primary" onClick={runNewsroomAgent} disabled={busy}>
            {busy ? "Đang lấy tin và dựng video…" : "Lấy tin mới và tạo video"}
            <span aria-hidden="true">↗</span>
          </button>
          <button className="button button-secondary" onClick={generateDrafts} disabled={busy || approvedCount === 0}>
            Xuất bản nháp
          </button>
        </div>
      </section>

      <section className="stats-grid" aria-label="Thống kê duyệt">
        {FILTERS.map(([key, label]) => (
          <button
            key={key}
            className={"stat-card " + (filter === key ? "stat-card-active" : "")}
            onClick={() => setFilter(key)}
          >
            <span>{label}</span>
            <strong>{counts[key] ?? 0}</strong>
          </button>
        ))}
      </section>

      {agentRun && <section className="agent-report" aria-label="Kết quả lấy tin">
        <strong>Kết quả lần chạy</strong>
        {agentRun.feed_errors?.map((item, index) => <p className="agent-report-error" key={`feed-${index}`}>RSS: {item}</p>)}
        {agentRun.processed?.map((item) => <p key={item.story_id}>
          <span>{item.status === "complete" ? "✓" : item.status === "error" ? "!" : "·"}</span>
          <b>{item.title}</b> — {item.reason || STEP_LABEL[item.step] || item.status}
        </p>)}
        {agentRun.processed?.length === 0 && <p>Không có story mới cần xử lý. {agentRun.deferred || 0} story đang chờ; {agentRun.reused_completed || 0} story đã hoàn tất được giữ lại.</p>}
      </section>}

      <section className="workspace">
        <aside className="story-list-panel">
          <div className="list-heading">
            <div>
              <p className="eyebrow">HÀNG ĐỢI BIÊN TẬP</p>
              <h3>{visibleItems.length} story</h3>
            </div>
            <span className="queue-count">{counts.unreviewed ?? 0} chờ duyệt</span>
          </div>
          <label className="search-box">
            <span aria-hidden="true">⌕</span>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Tìm đội bóng, cầu thủ, nguồn…" />
            <kbd>/</kbd>
          </label>
          <div className="story-list">
            {visibleItems.map((story) => (
              <button
                key={story.story_id}
                className={"story-card " + (selectedId === story.story_id ? "story-card-selected" : "")}
                onClick={() => selectStory(story)}
              >
                <div className="story-card-meta">
                  <span className={"status-dot status-" + story.review_status} />
                  <span>{STATUS_LABEL[story.review_status] || "Chưa rõ"}</span>
                  <time>{formatDate(story.latest_published_at)}</time>
                </div>
                <strong>{story.title}</strong>
                <div className="story-card-footer">
                  <span>{story.competition_candidate === "Premier League" ? "Ngoại hạng Anh?" : "Cần xác định giải"}</span>
                  <span>{story.source_count} nguồn · {story.article_count} bài</span>
                </div>
              </button>
            ))}
            {visibleItems.length === 0 && <div className="empty-state">Không có story phù hợp bộ lọc.</div>}
          </div>
        </aside>

        <section className="detail-panel">
          {selected && form ? (
            <>
              <div className="detail-header">
                <div>
                  <div className="detail-kicker">
                    <span className={"status-pill status-pill-" + selected.review_status}>
                      {STATUS_LABEL[selected.review_status] || selected.review_status}
                    </span>
                    <span>{selected.source_count} nhà xuất bản độc lập · {selected.article_count} bài</span>
                  </div>
                  <h3>{selected.title}</h3>
                </div>
                <span className="story-id">{selected.story_id}</span>
              </div>
              {selected.agent_error && <div className="notice notice-error">
                Dừng ở bước {STEP_LABEL[selected.agent_step] || selected.agent_step || "chưa rõ"}: {selected.agent_error}
              </div>}

              <div className="source-section">
                <div className="section-heading">
                  <div><p className="eyebrow">BẰNG CHỨNG GỐC</p><h4>Nguồn của câu chuyện</h4></div>
                  <span className="source-total">{selected.sources?.length ?? 0} liên kết</span>
                </div>
                <div className="source-list">
                  {selected.sources?.map((source, index) => (
                    <a className="source-row" key={source.source_url} href={source.source_url} target="_blank" rel="noreferrer">
                      <span className="source-index">{String(index + 1).padStart(2, "0")}</span>
                      <span className="source-info">
                        <strong>{source.publisher || source.source_name}</strong>
                        <small>{source.source_name} · {formatDate(source.published_at)}</small>
                      </span>
                      <span aria-hidden="true" className="external-link">↗</span>
                    </a>
                  ))}
                </div>
              </div>

              <div className="review-form">
                <div className="section-heading">
                  <div><p className="eyebrow">QUYẾT ĐỊNH BIÊN TẬP</p><h4>Xác nhận trước khi viết</h4></div>
                  <span className="candidate-tag">Gợi ý: {selected.competition_candidate === "Premier League" ? "Ngoại hạng Anh" : "chưa rõ giải"}</span>
                </div>

                <label className="confirm-check">
                  <input
                    type="checkbox"
                    checked={form.competition_confirmed}
                    onChange={(event) => updateField("competition_confirmed", event.target.checked)}
                  />
                  <span className="check-copy">
                    <strong>Xác nhận câu chuyện thuộc Ngoại hạng Anh</strong>
                    <small>Không chọn nếu bài chỉ nhắc đến một đội bóng nhưng sự kiện không liên quan giải đấu.</small>
                  </span>
                </label>

                <label className="field-label" htmlFor="review-status">Trạng thái xác minh</label>
                <select id="review-status" value={form.review_status} onChange={(event) => updateField("review_status", event.target.value)}>
                  <option value="unreviewed">Chờ duyệt</option>
                  <option value="supported">Đã xác nhận</option>
                  <option value="uncertain">Chưa chắc chắn</option>
                  <option value="rejected">Loại bỏ</option>
                </select>

                <label className="field-label" htmlFor="summary-vi">Tóm tắt tiếng Việt</label>
                <textarea
                  id="summary-vi"
                  rows="5"
                  value={form.summary_vi}
                  onChange={(event) => updateField("summary_vi", event.target.value)}
                  placeholder="Viết tóm tắt của bạn dựa trên các nguồn đã kiểm tra…"
                  maxLength="3000"
                />
                <div className="field-hint">{form.summary_vi.length}/3000 · Kịch bản chỉ dùng nội dung bạn nhập ở đây.</div>

                <label className="field-label" htmlFor="evidence-notes">Ghi chú kiểm chứng</label>
                <textarea
                  id="evidence-notes"
                  rows="3"
                  value={form.evidence_notes}
                  onChange={(event) => updateField("evidence_notes", event.target.value)}
                  placeholder="Nguồn nào xác nhận? Có điểm nào còn mâu thuẫn?"
                  maxLength="3000"
                />

                <div className="form-footer">
                  <span className={isDirty ? "unsaved-label" : "saved-label"}>{isDirty ? "Có thay đổi chưa lưu" : "Đã đồng bộ"}</span>
                  <button className="button button-primary" onClick={saveReview} disabled={busy || !isDirty}>Lưu quyết định</button>
                </div>
              </div>

              <div className="script-workspace">
                <div className="section-heading">
                  <div><p className="eyebrow">AI TẠO CHỮ · AI RÀ SOÁT</p><h4>Tóm tắt trên video</h4></div>
                  {currentText && selected.ai_review_status && <span className={"ai-review-tag ai-review-" + selected.ai_review_status}>
                    {scriptChanged ? "Bản sửa chờ rà soát" : selected.ai_review_status === "pass" ? "AI đã duyệt" : selected.ai_review_status === "editor_approved" ? "Biên tập đã xác nhận" : "AI đề xuất kiểm tra"}
                  </span>}
                </div>
                <p className="script-note">Video gồm ảnh liên quan đến tin, âm thanh và ba dòng tóm tắt. Bạn có thể sửa chữ, xem trước MP4 rồi đăng lên YouTube Shorts.</p>
                <button className="button button-secondary" onClick={generateSelectedScript} disabled={busy || !["supported", "uncertain"].includes(selected.review_status)}>
                  {busy ? "Đang xử lý…" : currentText ? "Tạo lại ba dòng bằng AI" : "AI tạo và rà soát chữ"}
                </button>
                {currentText && <>
                  <label className="field-label" htmlFor="generated-script">Ba dòng chữ (mỗi dòng tối đa 48 ký tự)</label>
                  <textarea id="generated-script" rows="4" value={scriptText} onChange={(event) => setScriptText(event.target.value)} maxLength="300" />
                  {selected.ai_review_notes && <div className="ai-review-notes"><strong>Ghi chú rà soát:</strong> {selected.ai_review_notes}</div>}
                  <div className="script-actions">
                    <span className={approvedText ? "saved-label" : "unsaved-label"}>
                      {approvedText && !scriptChanged ? "AI đã rà soát chữ" : scriptChanged ? "Chữ đã sửa, chờ rà soát" : "Chưa được AI rà soát"}
                    </span>
                    <button className="button button-primary" onClick={reviewScript} disabled={busy || (!scriptChanged && approvedText) || scriptText.trim().length < 3}>AI rà soát ba dòng chữ</button>
                  </div>
                  {!approvedText && <button className="button button-secondary" onClick={() => reviewScript(true)} disabled={busy || scriptText.trim().length < 3}>
                    Tôi đã kiểm tra chữ — cho phép dựng video
                  </button>}
                  <div className="video-actions">
                    <button className="button button-primary" onClick={renderSelectedVideo} disabled={busy || !approvedText || scriptChanged}>
                      {busy ? "Đang dựng…" : selected.video_url ? "Dựng lại video" : "Tạo video ảnh + nhạc"}
                    </button>
                    <span>Video dọc 9:16 · ảnh tin · âm thanh · chữ tóm tắt · tự xóa sau 2 giờ</span>
                  </div>
                  {selected.agent_step && selected.agent_step !== "complete" && <div className="field-hint">
                    Tiến trình gần nhất: {STEP_LABEL[selected.agent_step] || selected.agent_step}
                    {selected.agent_error ? ` — ${selected.agent_error}` : ""}
                  </div>}
                  {currentText && selected.video_url && <div className="video-preview">
                    <video controls playsInline src={selected.video_url} />
                    <strong>Xem trước video</strong>
                    {selected.image_page_url && <a href={selected.image_page_url} target="_blank" rel="noreferrer">Mở trang ảnh và giấy phép ↗</a>}
                    <a href={selected.video_url} download={`${selected.story_id}.mp4`}>Tải video MP4</a>
                    {selected.credits_url && <a href={selected.credits_url} download={`${selected.story_id}_credits.txt`}>Tải thông tin ghi công và license</a>}
                    <div className="youtube-publish">
                      <p className="field-label">Đăng lên YouTube Shorts</p>
                      {selected.youtube_url ? <>
                        <a href={selected.youtube_url} target="_blank" rel="noreferrer">Mở video trên YouTube ↗</a>
                        <span className="field-hint">Trạng thái quyền xem: {selected.youtube_privacy_status || "chưa rõ"}</span>
                      </> : <>
                        <button className="button button-primary" onClick={uploadSelectedVideo} disabled={busy || isDirty || !approvedText || !youtubeStatus.connected}>
                          {busy ? "Đang tải lên…" : "Đăng thẳng lên YouTube Shorts"}
                        </button>
                        {youtubeStatus.configured && !youtubeStatus.connected
                          ? <button className="button button-secondary" onClick={connectYouTube} disabled={busy}>Kết nối tài khoản YouTube</button>
                          : !youtubeStatus.configured
                            ? <span className="field-hint">Thêm YOUTUBE_CLIENT_ID và YOUTUBE_CLIENT_SECRET vào `.env`, khởi động lại backend rồi kết nối tài khoản.</span>
                            : null}
                      </>}
                      {selected.youtube_upload_error && <div className="ai-review-notes">Lần đăng trước lỗi: {selected.youtube_upload_error}</div>}
                      {!selected.youtube_url && <span className="field-hint">Nút đăng sẽ đặt video ở chế độ công khai. Google có thể giới hạn video ở chế độ riêng tư cho đến khi OAuth project được audit.</span>}
                    </div>
                  </div>}
                </>}
              </div>
            </>
          ) : (
            <div className="empty-detail">
              <div className="empty-icon">✦</div>
              <h3>Chọn một story để duyệt</h3>
              <p>Danh sách nguồn và biểu mẫu biên tập sẽ hiện ở đây.</p>
            </div>
          )}
        </section>
      </section>

      {draftResult && (
        <section className="draft-preview">
          <div className="section-heading">
            <div><p className="eyebrow">BẢN NHÁP ĐÃ TẠO</p><h3>{draftResult.created} kịch bản</h3></div>
            <span>{draftResult.output_path}</span>
          </div>
          <pre>{draftResult.content}</pre>
        </section>
      )}
      </>}
      <footer className="app-footer">
        <span>Newsroom prototype</span>
        <span>Quyết định duyệt được lưu trong review_queue.json</span>
      </footer>
    </main>
  );
}

function formatDate(value) {
  if (!value) return "Chưa có ngày";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Chưa có ngày";
  return new Intl.DateTimeFormat("vi-VN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export default App;
