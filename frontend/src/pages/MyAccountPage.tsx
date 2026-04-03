import { FormEvent, useEffect, useState } from "react";

import {
  useChangeMyPassword,
  useDeleteMyAvatar,
  useMe,
  useUpdateMyPreferences,
  useUpdateMyProfile,
  useUploadMyAvatar,
} from "../api/auth";
import { UserAvatar } from "../components/UserAvatar";
import { useI18n } from "../lib/i18n";
import { useSessionStore } from "../stores/sessionStore";

function validatePassword(value: string) {
  if (value.length < 8) {
    return "Password must have at least 8 characters.";
  }
  if (!/[A-Z]/.test(value)) {
    return "Password must include at least one uppercase letter.";
  }
  if (!/[0-9]/.test(value)) {
    return "Password must include at least one number.";
  }
  if (!/[^A-Za-z0-9]/.test(value)) {
    return "Password must include at least one special character.";
  }
  return null;
}

function extractErrorMessage(error: unknown) {
  if (typeof error === "object" && error && "response" in error) {
    const response = (error as { response?: { data?: unknown } }).response;
    const data = response?.data;
    if (typeof data === "object" && data && "detail" in data) {
      const detail = (data as { detail?: unknown }).detail;
      if (typeof detail === "string") {
        return detail;
      }
      if (Array.isArray(detail) && detail.length) {
        const first = detail[0] as { msg?: string } | undefined;
        if (first?.msg) {
          return first.msg;
        }
      }
    }
  }
  return error instanceof Error ? error.message : "Request failed";
}

export function MyAccountPage() {
  const token = useSessionStore((state) => state.token);
  const setUser = useSessionStore((state) => state.setUser);
  const currentUser = useSessionStore((state) => state.user);
  const { t } = useI18n();
  const { data: me } = useMe(Boolean(token));
  const updateProfile = useUpdateMyProfile();
  const updatePreferences = useUpdateMyPreferences();
  const changePassword = useChangeMyPassword();
  const uploadAvatar = useUploadMyAvatar();
  const deleteAvatar = useDeleteMyAvatar();
  const [displayName, setDisplayName] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [profileMessage, setProfileMessage] = useState<string | null>(null);
  const [passwordMessage, setPasswordMessage] = useState<string | null>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);

  useEffect(() => {
    if (me) {
      setUser(me);
      setDisplayName(me.display_name);
    }
  }, [me, setUser]);

  async function onSaveProfile(event: FormEvent) {
    event.preventDefault();
    const normalized = displayName.trim();
    if (!normalized) {
      setProfileMessage(`${t("users.displayName")} cannot be empty.`);
      return;
    }
    try {
      const updated = await updateProfile.mutateAsync({ display_name: normalized });
      setUser(updated);
      setProfileMessage(t("account.profileUpdated"));
    } catch (error) {
      setProfileMessage(extractErrorMessage(error));
    }
  }

  async function onSavePassword(event: FormEvent) {
    event.preventDefault();
    setPasswordMessage(null);
    const strengthError = validatePassword(newPassword);
    if (strengthError) {
      setPasswordError(strengthError);
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("Password confirmation does not match.");
      return;
    }
    if (!currentPassword.trim()) {
      setPasswordError("Current password is required.");
      return;
    }
    try {
      await changePassword.mutateAsync({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordError(null);
      setPasswordMessage(t("account.passwordUpdated"));
    } catch (error) {
      setPasswordError(extractErrorMessage(error));
    }
  }

  async function onAvatarSelected(file: File | null) {
    if (!file) {
      return;
    }
    try {
      const updated = await uploadAvatar.mutateAsync(file);
      setUser(updated);
      setProfileMessage(t("account.iconUpdated"));
    } catch (error) {
      setProfileMessage(extractErrorMessage(error));
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[0.72fr_1.28fr]">
      <section className="panel-frame p-6">
        <p className="panel-label">{t("account.myAccount")}</p>
        <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("account.operatorProfile")}</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
          {t("account.profileCopy")}
        </p>

        <div className="mt-6 flex items-start gap-4">
          {currentUser ? <UserAvatar user={currentUser} sizeClass="h-20 w-20" className="shrink-0" /> : null}
          <div className="min-w-0">
            <p className="panel-label">{t("account.username")}</p>
            <p className="mt-2 text-lg text-[var(--text-display)]">{currentUser?.username ?? "..."}</p>
            <p className="mt-2 text-sm uppercase tracking-[0.1em] text-[var(--text-secondary)]">
              {currentUser?.role ?? "offline"}
            </p>
          </div>
        </div>

        <form className="mt-6 space-y-4" onSubmit={onSaveProfile}>
          <label className="panel-field">
            <span className="panel-label">{t("users.displayName")}</span>
            <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("account.myTheme")}</span>
            <select
              value={currentUser?.theme_preference ?? "default"}
              onChange={(event) => {
                void updatePreferences
                  .mutateAsync({
                    theme_preference: event.target.value as "default" | "dark" | "light" | "system",
                  })
                  .then((updated) => {
                    setUser(updated);
                    setProfileMessage(`${t("account.myTheme")} updated.`);
                  })
                  .catch((error: unknown) => {
                    setProfileMessage(extractErrorMessage(error));
                  });
              }}
            >
              <option value="default">{t("common.useInstanceDefault")}</option>
              <option value="dark">{t("common.dark")}</option>
              <option value="light">{t("common.light")}</option>
              <option value="system">{t("common.system")}</option>
            </select>
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("account.myLanguage")}</span>
            <select
              value={currentUser?.locale_preference ?? "default"}
              onChange={(event) => {
                void updatePreferences
                  .mutateAsync({
                    locale_preference: event.target.value as "default" | "en" | "es",
                  })
                  .then((updated) => {
                    setUser(updated);
                    setProfileMessage(`${t("account.myLanguage")} updated.`);
                  })
                  .catch((error: unknown) => {
                    setProfileMessage(extractErrorMessage(error));
                  });
              }}
            >
              <option value="default">{t("common.useInstanceDefault")}</option>
              <option value="en">{t("common.english")}</option>
              <option value="es">{t("common.spanish")}</option>
            </select>
          </label>

          <div className="border-t border-[var(--border)] pt-4">
            <p className="panel-label">{t("account.operatorIcon")}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <label className="panel-button-secondary cursor-pointer">
                {t("users.uploadIcon")}
                <input
                  className="hidden"
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  onChange={(event) => void onAvatarSelected(event.target.files?.[0] ?? null)}
                />
              </label>
              <button
                type="button"
                className="panel-button-secondary"
                onClick={() =>
                  void deleteAvatar
                    .mutateAsync()
                    .then((updated) => {
                      setUser(updated);
                      setProfileMessage(t("account.iconRemoved"));
                    })
                    .catch((error: unknown) => {
                      setProfileMessage(extractErrorMessage(error));
                    })
                }
                disabled={!currentUser?.has_avatar || deleteAvatar.isPending}
              >
                {t("users.removeIcon")}
              </button>
            </div>
          </div>

          <button className="panel-button-primary w-full" type="submit" disabled={updateProfile.isPending}>
            {t("account.saveProfile")}
          </button>

          {profileMessage ? (
            <p className={`text-sm ${profileMessage.includes("updated") || profileMessage.includes("removed") || profileMessage.includes("actualiz") || profileMessage.includes("elim") ? "text-[var(--success)]" : "text-[var(--accent)]"}`}>
              {profileMessage}
            </p>
          ) : null}
        </form>
      </section>

      <section className="panel-frame p-6">
        <p className="panel-label">{t("account.security")}</p>
        <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("account.changePassword")}</h2>
        <p className="mt-3 max-w-[44rem] text-sm leading-6 text-[var(--text-secondary)]">
          {t("account.passwordCopy")}
        </p>

        <form className="mt-6 space-y-4" onSubmit={onSavePassword}>
          <label className="panel-field">
            <span className="panel-label">{t("account.currentPassword")}</span>
            <input
              type="password"
              value={currentPassword}
              onChange={(event) => {
                setPasswordError(null);
                setCurrentPassword(event.target.value);
              }}
              autoComplete="current-password"
            />
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("account.newPassword")}</span>
            <input
              type="password"
              value={newPassword}
              onChange={(event) => {
                setPasswordError(null);
                setNewPassword(event.target.value);
              }}
              autoComplete="new-password"
            />
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("account.confirmPassword")}</span>
            <input
              type="password"
              value={confirmPassword}
              onChange={(event) => {
                setPasswordError(null);
                setConfirmPassword(event.target.value);
              }}
              autoComplete="new-password"
            />
          </label>
          <p className="text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">
            {t("users.passwordHint")}
          </p>
          <button className="panel-button-primary w-full" type="submit" disabled={changePassword.isPending}>
            {t("account.savePassword")}
          </button>
          {passwordError ? <p className="text-sm text-[var(--accent)]">{passwordError}</p> : null}
          {passwordMessage ? <p className="text-sm text-[var(--success)]">{passwordMessage}</p> : null}
        </form>
      </section>
    </div>
  );
}
