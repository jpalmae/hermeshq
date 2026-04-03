import { FormEvent, useMemo, useState } from "react";

import { useAgents } from "../api/agents";
import { useCreateUser, useDeleteUser, useDeleteUserAvatar, useUpdateUser, useUploadUserAvatar, useUsers } from "../api/users";
import { UserAvatar } from "../components/UserAvatar";
import { useSessionStore } from "../stores/sessionStore";

const emptyCreateForm = {
  username: "",
  display_name: "",
  password: "",
  role: "user",
  is_active: true,
  assigned_agent_ids: [] as string[],
};

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

export function UsersPage() {
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { data: agents } = useAgents();
  const { data: users } = useUsers(isAdmin);
  const createUser = useCreateUser();
  const deleteUser = useDeleteUser();
  const uploadUserAvatar = useUploadUserAvatar();
  const deleteUserAvatar = useDeleteUserAvatar();
  const updateUser = useUpdateUser();
  const [createForm, setCreateForm] = useState(emptyCreateForm);
  const [passwordDrafts, setPasswordDrafts] = useState<Record<string, string>>({});
  const [displayNameDrafts, setDisplayNameDrafts] = useState<Record<string, string>>({});
  const [createError, setCreateError] = useState<string | null>(null);
  const [createInfo, setCreateInfo] = useState<string | null>(null);
  const [rowMessages, setRowMessages] = useState<Record<string, string | null>>({});

  const agentOptions = useMemo(
    () => (agents ?? []).map((agent) => ({ id: agent.id, label: agent.friendly_name || agent.name })),
    [agents],
  );

  if (currentUser && !isAdmin) {
    return (
      <section className="panel-frame p-6">
        <p className="panel-label">Users</p>
        <h2 className="mt-2 text-3xl text-[var(--text-display)]">Admin access required</h2>
        <p className="mt-4 max-w-[42rem] text-sm leading-6 text-[var(--text-secondary)]">
          User management is restricted to admins. Standard users can only operate agents assigned to them.
        </p>
      </section>
    );
  }

  async function onCreateUser(event: FormEvent) {
    event.preventDefault();
    setCreateError(null);
    setCreateInfo(null);
    const passwordError = validatePassword(createForm.password.trim());
    if (passwordError) {
      setCreateError(passwordError);
      return;
    }
    try {
      await createUser.mutateAsync(createForm);
      setCreateForm(emptyCreateForm);
      setCreateInfo("User created.");
    } catch (error) {
      setCreateError(extractErrorMessage(error));
    }
  }

  async function onDeleteUser(userId: string, username: string) {
    const confirmed = window.confirm(`Delete user "${username}"?`);
    if (!confirmed) {
      return;
    }
    try {
      await deleteUser.mutateAsync(userId);
    } catch (error) {
      window.alert(extractErrorMessage(error));
    }
  }

  async function onAvatarSelected(userId: string, file: File | null) {
    if (!file) {
      return;
    }
    try {
      await uploadUserAvatar.mutateAsync({ userId, file });
    } catch (error) {
      window.alert(extractErrorMessage(error));
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[0.68fr_1.32fr]">
      <form className="panel-frame p-6" onSubmit={onCreateUser}>
        <p className="panel-label">Users</p>
        <h2 className="mt-2 text-3xl text-[var(--text-display)]">Create operator</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
          Admins can create instance users and pre-assign the agents they are allowed to manage.
        </p>
        <div className="mt-6 space-y-4">
          <label className="panel-field">
            <span className="panel-label">Username</span>
            <input value={createForm.username} onChange={(event) => setCreateForm((current) => ({ ...current, username: event.target.value }))} />
          </label>
          <label className="panel-field">
            <span className="panel-label">Display name</span>
            <input value={createForm.display_name} onChange={(event) => setCreateForm((current) => ({ ...current, display_name: event.target.value }))} />
          </label>
          <label className="panel-field">
            <span className="panel-label">Password</span>
            <input
              type="password"
              minLength={8}
              value={createForm.password}
              onChange={(event) => {
                setCreateError(null);
                setCreateInfo(null);
                setCreateForm((current) => ({ ...current, password: event.target.value }));
              }}
            />
            <p className="mt-2 text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">
              Minimum 8 characters / 1 uppercase / 1 number / 1 special
            </p>
          </label>
          <label className="panel-field">
            <span className="panel-label">Role</span>
            <select value={createForm.role} onChange={(event) => setCreateForm((current) => ({ ...current, role: event.target.value as "admin" | "user" }))}>
              <option value="user">User</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <label className="panel-field">
            <span className="panel-label">Assigned agents</span>
            <select
              multiple
              value={createForm.assigned_agent_ids}
              onChange={(event) =>
                setCreateForm((current) => ({
                  ...current,
                  assigned_agent_ids: Array.from(event.target.selectedOptions, (option) => option.value),
                }))
              }
              className="min-h-40"
            >
              {agentOptions.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.label}
                </option>
              ))}
            </select>
          </label>
          <label className="mt-2 flex items-center gap-3 text-sm text-[var(--text-secondary)]">
            <input
              type="checkbox"
              checked={createForm.is_active}
              onChange={(event) => setCreateForm((current) => ({ ...current, is_active: event.target.checked }))}
              className="h-4 w-4"
            />
            Active account
          </label>
          <button className="panel-button-primary w-full" type="submit" disabled={createUser.isPending}>
            {createUser.isPending ? "[LOADING]" : "Create user"}
          </button>
          {createError ? <p className="text-sm text-[var(--accent)]">{createError}</p> : null}
          {createInfo ? <p className="text-sm text-[var(--success)]">{createInfo}</p> : null}
        </div>
      </form>

      <section className="panel-frame p-6">
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">Directory</p>
            <h2 className="mt-2 text-3xl text-[var(--text-display)]">Access registry</h2>
          </div>
          <p className="panel-label">{users?.length ?? 0} accounts</p>
        </div>
        <div className="mt-2">
          {(users ?? []).map((user) => (
            <article key={user.id} className="border-b border-[var(--border)] py-5">
              <div className="grid gap-5 xl:grid-cols-[1fr_0.9fr]">
                <div className="flex items-start gap-4">
                  <UserAvatar user={user} sizeClass="h-14 w-14" className="shrink-0" />
                  <div className="min-w-0 flex-1">
                    <p className="panel-label">{user.username}</p>
                    <div className="mt-2 grid gap-3">
                      <label className="panel-field">
                        <span className="panel-label">Display name</span>
                        <input
                          value={displayNameDrafts[user.id] ?? user.display_name}
                          onChange={(event) =>
                            setDisplayNameDrafts((current) => ({ ...current, [user.id]: event.target.value }))
                          }
                        />
                      </label>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          className="panel-button-secondary"
                          onClick={async () => {
                            const displayName = (displayNameDrafts[user.id] ?? user.display_name).trim();
                            if (!displayName) {
                              setRowMessages((current) => ({ ...current, [user.id]: "Display name cannot be empty." }));
                              return;
                            }
                            try {
                              await updateUser.mutateAsync({
                                userId: user.id,
                                payload: { display_name: displayName },
                              });
                              setRowMessages((current) => ({ ...current, [user.id]: "Display name updated." }));
                            } catch (error) {
                              setRowMessages((current) => ({ ...current, [user.id]: extractErrorMessage(error) }));
                            }
                          }}
                        >
                          Save display name
                        </button>
                      </div>
                      <div className="border-t border-[var(--border)] pt-3">
                        <p className="panel-label">Operator icon</p>
                        <div className="mt-3 flex flex-wrap gap-2">
                          <label className="panel-button-secondary cursor-pointer">
                            Upload icon
                            <input
                              className="hidden"
                              type="file"
                              accept="image/png,image/jpeg,image/webp"
                              onChange={(event) => void onAvatarSelected(user.id, event.target.files?.[0] ?? null)}
                            />
                          </label>
                          <button
                            type="button"
                            className="panel-button-secondary"
                            onClick={() => void deleteUserAvatar.mutateAsync(user.id)}
                            disabled={!user.has_avatar || deleteUserAvatar.isPending}
                          >
                            Remove icon
                          </button>
                        </div>
                      </div>
                    </div>
                    <p className="mt-3 text-sm uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                      {user.role} / {user.is_active ? "active" : "inactive"}
                    </p>
                    <p className="mt-3 text-sm text-[var(--text-secondary)]">
                      Assigned:{" "}
                      {user.assigned_agent_ids.length
                        ? user.assigned_agent_ids
                            .map((agentId) => agentOptions.find((option) => option.id === agentId)?.label ?? agentId)
                            .join(", ")
                        : "No agents"}
                    </p>
                  </div>
                </div>
                <div className="space-y-4">
                  <label className="panel-field">
                    <span className="panel-label">Role</span>
                    <select
                      value={user.role}
                      onChange={(event) =>
                        void updateUser.mutateAsync({
                          userId: user.id,
                          payload: { role: event.target.value },
                        })
                      }
                    >
                      <option value="user">User</option>
                      <option value="admin">Admin</option>
                    </select>
                  </label>
                  <label className="panel-field">
                    <span className="panel-label">Assigned agents</span>
                    <select
                      multiple
                      value={user.assigned_agent_ids}
                      onChange={(event) =>
                        void updateUser.mutateAsync({
                          userId: user.id,
                          payload: {
                            assigned_agent_ids: Array.from(event.target.selectedOptions, (option) => option.value),
                          },
                        })
                      }
                      className="min-h-32"
                    >
                      {agentOptions.map((agent) => (
                        <option key={agent.id} value={agent.id}>
                          {agent.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="grid gap-3 md:grid-cols-[1fr_auto_auto] md:items-end">
                    <label className="panel-field">
                      <span className="panel-label">Reset password</span>
                      <input
                        type="password"
                        minLength={8}
                        value={passwordDrafts[user.id] ?? ""}
                        onChange={(event) =>
                          {
                            setRowMessages((current) => ({ ...current, [user.id]: null }));
                            setPasswordDrafts((current) => ({ ...current, [user.id]: event.target.value }));
                          }
                        }
                        placeholder="New password"
                      />
                    </label>
                    <button
                      type="button"
                      className="panel-button-secondary"
                      onClick={async () => {
                        const password = (passwordDrafts[user.id] ?? "").trim();
                        if (!password) {
                          return;
                        }
                        const passwordError = validatePassword(password);
                        if (passwordError) {
                          setRowMessages((current) => ({ ...current, [user.id]: passwordError }));
                          return;
                        }
                        try {
                          await updateUser.mutateAsync({ userId: user.id, payload: { password } });
                          setPasswordDrafts((current) => ({ ...current, [user.id]: "" }));
                          setRowMessages((current) => ({ ...current, [user.id]: "Password updated." }));
                        } catch (error) {
                          setRowMessages((current) => ({ ...current, [user.id]: extractErrorMessage(error) }));
                        }
                      }}
                    >
                      Save password
                    </button>
                    <button
                      type="button"
                      className="panel-button-secondary"
                      onClick={() =>
                        void updateUser.mutateAsync({
                          userId: user.id,
                          payload: { is_active: !user.is_active },
                        })
                      }
                    >
                      {user.is_active ? "Deactivate" : "Activate"}
                    </button>
                  </div>
                  <div className="flex justify-end">
                    <button
                      type="button"
                      className="panel-button-secondary border-[var(--accent)] text-[var(--accent)]"
                      onClick={() => void onDeleteUser(user.id, user.username)}
                      disabled={currentUser?.id === user.id || deleteUser.isPending}
                    >
                      Delete user
                    </button>
                  </div>
                  {rowMessages[user.id] ? (
                    <p className={`text-sm ${rowMessages[user.id] === "Password updated." ? "text-[var(--success)]" : "text-[var(--accent)]"}`}>
                      {rowMessages[user.id]}
                    </p>
                  ) : null}
                </div>
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
