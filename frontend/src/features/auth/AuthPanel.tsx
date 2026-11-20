import { useState } from "react";

import type { User } from "../../types";
import { clearAccessToken, loginUser, registerUser } from "../../shared/services/api";

interface AuthPanelProps {
  user: User | null;
  onAuthenticated: (user: User) => void;
  onLoggedOut: () => void;
}

export function AuthPanel({ user, onAuthenticated, onLoggedOut }: AuthPanelProps) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (user) {
    return (
      <section className="auth-card auth-user-card">
        <div>
          <span className="eyebrow">当前用户</span>
          <strong>{user.display_name}</strong>
          <small>{user.email}</small>
        </div>
        <button
          type="button"
          onClick={() => {
            // 无状态访问令牌的退出操作是清除本地令牌并重置页面用户。
            clearAccessToken();
            onLoggedOut();
          }}
        >
          退出登录
        </button>
      </section>
    );
  }

  async function submit() {
    setBusy(true);
    setError("");
    try {
      const response = mode === "register"
        ? await registerUser(email.trim(), password, displayName.trim() || "DocPilot 用户")
        : await loginUser(email.trim(), password);
      onAuthenticated(response.user);
      setPassword("");
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "认证失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="auth-card">
      <div className="auth-heading">
        <div>
          <span className="eyebrow">账户</span>
          <h2>{mode === "login" ? "登录 DocPilot" : "创建 DocPilot 账户"}</h2>
        </div>
        <div className="auth-switch" role="tablist" aria-label="认证模式">
          <button type="button" className={mode === "login" ? "active" : ""} onClick={() => { setMode("login"); setError(""); }}>登录</button>
          <button type="button" className={mode === "register" ? "active" : ""} onClick={() => { setMode("register"); setError(""); }}>注册</button>
        </div>
      </div>
      <div className="auth-form">
        {mode === "register" && (
          <input aria-label="显示名称" placeholder="显示名称" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
        )}
        <input aria-label="邮箱" type="email" placeholder="邮箱" value={email} onChange={(event) => setEmail(event.target.value)} />
        <input aria-label="密码" type="password" minLength={8} placeholder={mode === "register" ? "密码（至少 8 位）" : "密码"} value={password} onChange={(event) => setPassword(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void submit()} />
        <button type="button" onClick={() => void submit()} disabled={busy || !email.trim() || password.length < (mode === "register" ? 8 : 1)}>
          {busy ? "处理中……" : mode === "login" ? "登录" : "注册并登录"}
        </button>
      </div>
      {error && <p className="auth-error" role="alert">{error}</p>}
      <small className="auth-hint">登录后可在同一浏览器中继续使用文档工作台。</small>
    </section>
  );
}
