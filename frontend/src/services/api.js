import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api',
  timeout: 15000,
})

api.interceptors.request.use((config) => {
  const token = window.localStorage.getItem('stockwatch_session')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use((response) => response, (error) => {
  if (error.response?.status === 401 && window.localStorage.getItem('stockwatch_session')
      && !error.config?.url?.includes('/auth/login')) {
    window.localStorage.removeItem('stockwatch_session')
    window.localStorage.removeItem('stockwatch_user')
    window.location.reload()
  }
  return Promise.reject(error)
})

export async function get(path, params) {
  const response = await api.get(path, { params })
  return response.data.data
}

export async function post(path, body = {}) {
  const response = await api.post(path, body)
  return response.data.data
}

export async function put(path, body = {}) {
  const response = await api.put(path, body)
  return response.data.data
}

export async function remove(path) {
  const response = await api.delete(path)
  return response.data.data
}

export function getErrorMessage(error) {
  return error.response?.data?.detail?.message || error.response?.data?.detail || error.message || 'Unable to reach the StockWatch-RX API.'
}
