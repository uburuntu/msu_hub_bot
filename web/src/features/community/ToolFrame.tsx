import type { ReactNode } from "react";
import type { Community } from "../../platform/communityTypes";
import { Icon } from "../../ui/Icon";

export function ToolFrame({
  title,
  subtitle,
  community,
  children,
}: {
  title: string;
  subtitle: string;
  community?: Community;
  children: ReactNode;
}) {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">
            <span className="eyebrow-dot" /> ДЛЯ СЕБЯ И СВОИХ
          </div>
          <h1>
            {title}
            <span className="heading-star">✳</span>
          </h1>
          <p>{subtitle}</p>
        </div>
      </div>
      {community && (
        <div className="context-strip">
          <Icon name="chat" size={18} />
          <div>
            <strong>{community.context.label}</strong>
            <p>Другой чат или тема? Откройте /app прямо там.</p>
          </div>
        </div>
      )}
      {children}
    </>
  );
}
export function LoadState({
  busy,
  error,
  retry,
}: {
  busy: boolean;
  error: string;
  retry: () => void;
}) {
  if (error)
    return (
      <div className="notice error" role="alert">
        {error}
        <button className="text-button" onClick={retry}>
          Попробовать ещё раз
        </button>
      </div>
    );
  return busy ? (
    <p className="tool-loading" role="status">
      Загружаем…
    </p>
  ) : null;
}
