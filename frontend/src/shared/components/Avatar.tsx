import { useTranslation } from "react-i18next";

import { RelativeTime } from "./RelativeTime";

export type AvatarProps = {
  actorType: string | null;
  className?: string;
  name: string | null;
};

export type AvatarWithTimestampProps = AvatarProps & {
  timestamp: string | null;
};

export function Avatar({ actorType, className = "", name }: AvatarProps) {
  const displayName = actorDisplayName(name, actorType);
  return (
    <span
      aria-hidden="true"
      className={`avatar ${className}`.trim()}
      data-actor-type={actorType || "unknown"}
      title={displayName}
    >
      {avatarInitials(displayName)}
    </span>
  );
}

export function AvatarWithTimestamp({
  actorType,
  className = "",
  name,
  timestamp,
}: AvatarWithTimestampProps) {
  const { i18n } = useTranslation();
  const displayName = actorDisplayName(name, actorType);
  return (
    <span className={`avatar-with-timestamp ${className}`.trim()}>
      <Avatar actorType={actorType} name={displayName} />
      <span className="avatar-with-timestamp-main">
        <span className="avatar-with-timestamp-name">{displayName}</span>
        {timestamp ? (
          <RelativeTime
            className="avatar-with-timestamp-time"
            value={timestamp}
            locale={i18n.language}
          />
        ) : null}
      </span>
    </span>
  );
}

export function actorDisplayName(name?: string | null, actorType?: string | null) {
  const trimmed = name?.trim();
  if (trimmed) {
    return trimmed;
  }
  if (actorType === "human" || actorType === "user") {
    return "Human editor";
  }
  if (actorType === "agent") {
    return "Agent worker";
  }
  if (!actorType) {
    return "Unknown actor";
  }
  return actorType
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function avatarInitials(name: string) {
  const words = name
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) {
    return "?";
  }
  if (words.length === 1) {
    return words[0].slice(0, 2).toLocaleUpperCase();
  }
  return `${words[0][0]}${words[words.length - 1][0]}`.toLocaleUpperCase();
}
