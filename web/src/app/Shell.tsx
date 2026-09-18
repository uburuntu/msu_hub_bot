import type { ReactNode } from "react";
import type { Session } from "../platform/types";
import { Icon } from "../ui/Icon";

export function Brand() {
  return (
    <span className="brand">
      <span className="brand-mark">
        <span>MSU</span>
        <strong>hub</strong>
      </span>
      <span className="brand-name">
        MSU Hub<span>свой бот для всего</span>
      </span>
    </span>
  );
}

export function Shell({
  session,
  children,
}: {
  session?: Session;
  children: ReactNode;
}) {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        К содержимому
      </a>
      <aside className="sidebar">
        <a className="brand-link" href="/" aria-label="MSU Hub — главная">
          <Brand />
        </a>
        <div className="nav-label">ВАШИ ИНСТРУМЕНТЫ</div>
        <nav aria-label="Инструменты">
          <a className="nav-item active" href="#main" aria-current="page">
            <Icon name="bell" />
            <span>Напоминания</span>
            <Icon name="arrow" size={16} />
          </a>
        </nav>
        <div className="sidebar-bottom">
          <span className="tiny-spark">
            <Icon name="sparkle" size={19} />
          </span>
          <p>
            Меньше «не забыть».
            <br />
            Больше всего остального.
          </p>
          <div className="sidebar-foot">
            Сделано для своих <span>✳</span>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <a
            className="mobile-brand brand-link"
            href="/"
            aria-label="MSU Hub — главная"
          >
            <Brand />
          </a>
          <div className="breadcrumb">
            Инструменты<span>/</span>
            <strong>Напоминания</strong>
          </div>
          {session && (
            <div className="user-chip">
              <span className="connection-dot" />
              <span className="user-name">{session.user.name}</span>
              <span className="avatar" aria-hidden="true">
                {Array.from(session.user.name)[0]?.toUpperCase() || "✳"}
              </span>
            </div>
          )}
        </header>
        <main id="main" tabIndex={-1}>
          {children}
        </main>
        <footer className="page-footer">
          <span>
            <Icon name="shield" size={14} /> Только ваши напоминания
          </span>
          <span>MSU Hub · полезное рядом</span>
        </footer>
      </div>
    </div>
  );
}
