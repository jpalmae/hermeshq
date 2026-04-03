import axios from "axios";
import type { AxiosError } from "axios";

import { useSessionStore } from "../stores/sessionStore";

const baseURL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api";

export const apiClient = axios.create({
  baseURL,
});

apiClient.interceptors.request.use((config) => {
  const token = useSessionStore.getState().token;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    const status = error.response?.status;
    const requestUrl = error.config?.url ?? "";
    const hasSession = Boolean(useSessionStore.getState().token);
    const isLoginAttempt = requestUrl.includes("/auth/login");
    const hadAuthHeader = Boolean(error.config?.headers?.Authorization);

    if (status === 401 && hasSession && hadAuthHeader && !isLoginAttempt) {
      useSessionStore.getState().logout();
    }

    return Promise.reject(error);
  },
);
