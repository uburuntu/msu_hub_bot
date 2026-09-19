import type { ReactNode } from "react";
import type { Session } from "../platform/types";
import { Icon } from "../ui/Icon";
import { sections } from "./navigation";
import type { Section } from "./navigation";

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
  section = "reminders",
  onNavigate,
}: {
  session?: Session;
  children: ReactNode;
  section?: Section;
  onNavigate?: (section: Section) => void;
}) {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        К содержимому
      </a>
      <aside className="sidebar">
        <a className="brand-link" href="#main" aria-label="MSU Hub — главная">
          <Brand />
        </a>
        <div className="nav-label">ВАШИ ИНСТРУМЕНТЫ</div>
        {onNavigate && <Navigation section={section} onNavigate={onNavigate} />}
        <div className="sidebar-bottom">
          <span className="tiny-spark">
            <Icon name="sparkle" size={19} />
          </span>
          <p>
            Полезное, весёлое.
            <br />
            Всё для своих.
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
            href="#main"
            aria-label="MSU Hub — главная"
          >
            <Brand />
          </a>
          <div className="breadcrumb">
            Инструменты<span>/</span>
            <strong>{sections[section].label}</strong>
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
        {onNavigate && (
          <div className="mobile-navigation">
            <Navigation section={section} onNavigate={onNavigate} />
          </div>
        )}
        <main id="main" tabIndex={-1}>
          {children}
        </main>
        <footer className="page-footer">
          <span>
            <Icon name="shield" size={14} /> Доступ через Telegram
          </span>
          <span>MSU Hub · полезное рядом</span>
        </footer>
      </div>
    </div>
  );
}

function Navigation({
  section,
  onNavigate,
}: {
  section: Section;
  onNavigate: (section: Section) => void;
}) {
  return (
    <nav aria-label="Инструменты">
      {(Object.keys(sections) as Section[]).map((key) => (
        <button
          key={key}
          className={`nav-item ${section === key ? "active" : ""}`}
          aria-current={section === key ? "page" : undefined}
          onClick={() => onNavigate(key)}
        >
          <Icon name={sections[key].icon} />
          <span>{sections[key].label}</span>
        </button>
      ))}
    </nav>
  );
}
