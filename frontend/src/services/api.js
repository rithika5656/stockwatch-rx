import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api',
  timeout: 15000,
})

export async function get(path, params) {
  const response = await api.get(path, { params })
  return response.data.data
}

export async function post(path, body = {}) {
  const response = await api.post(path, body)
  return response.data.data
}

export function getErrorMessage(error) {
  return error.response?.data?.detail?.message || error.response?.data?.detail || error.message || 'Unable to reach the StockWatch-RX API.'
}
