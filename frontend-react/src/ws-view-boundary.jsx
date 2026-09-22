import React from "react";

/* 加载态按原因说话：等数据（作品列表还没从本机服务回来）和等模块（第一次打开这个页面要先下载它）
   是两件事，过去一律说「首次进入时需要加载这一模块」。 */
function ViewLoading({ label = "页面", reason = "module" }) {
  const data = reason === "data";
  return (
    <section className="ws-view-loading" role="status" aria-live="polite">
      <span className="ws-view-loading-mark" aria-hidden="true" />
      <strong>{data ? `正在读取${label}…` : `正在打开${label}…`}</strong>
      <span>{data ? "正在从本机服务读取数据。" : "第一次打开这个页面，需要先把它载入。"}</span>
    </section>
  );
}

function ProjectRequired({ label = "这个页面", onCreate, onGoHome }) {
  return (
    <section className="ws-project-required" role="status" data-testid="project-required">
      <h2>先创建一部作品</h2>
      <p>「{label}」里的内容必须归属于明确作品，系统不会把数据写进匿名或加载占位空间。</p>
      <div className="ws-project-required-actions">
        <button type="button" className="btn btn-accent" onClick={onCreate}>创建第一部作品</button>
        <button type="button" className="btn btn-ghost" onClick={onGoHome}>回到主页</button>
      </div>
    </section>
  );
}

/* 错误隔离层。
   · 页面级（默认）：把故障限制在当前页面，给重试 / 回主页 / 重新加载，错误原文收在「错误详情」里；
   · silent：给外壳上的小部件（侧栏里的同步与恢复入口这类）用——出错时安静地不渲染，
     绝不能把整页的错误块画到当前页面上。两种都会派发 ws:view-error。 */
class ViewErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
    this.retry = this.retry.bind(this);
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    try {
      window.dispatchEvent(new CustomEvent("ws:view-error", {
        detail: {
          message: error instanceof Error ? error.message : String(error || "未知错误"),
          componentStack: info?.componentStack || "",
          view: this.props.resetKey || "unknown",
        },
      }));
    } catch (ignored) {
      // 错误隔离层本身不能因为遥测不可用而再次崩溃。
    }
  }

  componentDidUpdate(previousProps) {
    if (this.state.error && previousProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  retry() {
    // 先让调用方重建失败的懒加载入口（React.lazy 会缓存失败），再清掉错误重新渲染。
    this.props.onRetry?.();
    this.setState({ error: null });
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.silent) return this.props.fallback ?? null;
    const detail = error instanceof Error ? error.message : String(error || "");
    return (
      <section className="ws-view-error" role="alert" data-testid="view-error-boundary">
        <h2>这个页面没有正常打开</h2>
        <p>故障只限于这个页面，其他页面照常可用。先点「重试」；如果仍然打不开，「重新加载应用」会重新获取全部文件。</p>
        <div className="ws-view-error-actions">
          <button type="button" className="btn btn-accent" onClick={this.retry}>重试</button>
          <button type="button" className="btn btn-ghost" onClick={this.props.onGoHome}>回到主页</button>
          <button type="button" className="btn btn-quiet" onClick={() => window.location.reload()}>重新加载应用</button>
        </div>
        {detail ? (
          <details className="ws-view-error-details">
            <summary>错误详情</summary>
            <pre>{detail}</pre>
          </details>
        ) : null}
      </section>
    );
  }
}

export { ProjectRequired, ViewErrorBoundary, ViewLoading };
