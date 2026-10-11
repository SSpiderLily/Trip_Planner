import type { V4TripRequest } from '@/types/v4TripRequest'

export interface V4TripFormInput {
  city: string
  arrivalLocal: string | null
  departureLocal: string | null
  budgetAmount: number | string | null | undefined
  lodgingQuery: string
  arrivalPlaceQuery: string
  departurePlaceQuery: string
  returnBufferHours: number | string | null | undefined
  returnBufferMinutes: number | string | null | undefined
  preferences: readonly string[]
  remarks: string
}

export function buildV4TripRequest(input: V4TripFormInput, requestId: string): V4TripRequest {
  const city = input.city.trim()
  if (!city) throw new Error('请输入目的地城市。')
  if (!requestId.trim()) throw new Error('请求标识不能为空。')

  const arrivalAt = formatShanghaiDateTime(input.arrivalLocal, '请选择有效的到达日期和时间。')
  const departureAt = formatShanghaiDateTime(input.departureLocal, '请选择有效的离开日期和时间。')
  if (departureAt <= arrivalAt) throw new Error('离开时间必须晚于到达时间。')

  const budget = optionalNonNegativeNumber(input.budgetAmount, '全程预算')
  const returnBuffer = buildReturnBuffer(input.returnBufferHours, input.returnBufferMinutes)

  return {
    schema_version: 4,
    request_id: requestId.trim(),
    city,
    arrival_at: `${arrivalAt}+08:00`,
    departure_at: `${departureAt}+08:00`,
    timezone: 'Asia/Shanghai',
    preferences: [...new Set(input.preferences.map(value => value.trim()).filter(Boolean))],
    transport_preferences: ['transit', 'bicycling', 'walking'],
    arrival_place_query: optionalText(input.arrivalPlaceQuery),
    departure_place_query: optionalText(input.departurePlaceQuery),
    lodging_query: optionalText(input.lodgingQuery),
    explicit_return_buffer_s: returnBuffer,
    budget: {
      amount: budget,
      currency: 'CNY',
      scope: 'whole_trip',
      party_basis: 'unspecified',
      party_size: null,
    },
    remarks: input.remarks.trim(),
  }
}

export function createSingleFlightSubmitter<T>(send: (request: V4TripRequest) => Promise<T>) {
  let inFlight: Promise<T> | null = null
  let accepted: T | null = null

  return (request: V4TripRequest): Promise<T> => {
    if (accepted !== null) return Promise.resolve(accepted)
    if (inFlight) return inFlight

    const current = send(request).then(result => {
      accepted = result
      return result
    }).finally(() => {
      if (inFlight === current) inFlight = null
    })
    inFlight = current
    return current
  }
}

function formatShanghaiDateTime(value: string | null, errorMessage: string): string {
  if (!value) throw new Error(errorMessage)
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/)
  if (!match) throw new Error(errorMessage)

  const [, yearText, monthText, dayText, hourText, minuteText, secondText = '00'] = match
  const year = Number(yearText)
  const month = Number(monthText)
  const day = Number(dayText)
  const hour = Number(hourText)
  const minute = Number(minuteText)
  const second = Number(secondText)
  if (year < 1900 || year > 9999 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) {
    throw new Error(errorMessage)
  }
  const roundTrip = new Date(Date.UTC(year, month - 1, day, hour, minute, second))
  if (
    roundTrip.getUTCFullYear() !== year
    || roundTrip.getUTCMonth() + 1 !== month
    || roundTrip.getUTCDate() !== day
    || roundTrip.getUTCHours() !== hour
    || roundTrip.getUTCMinutes() !== minute
    || roundTrip.getUTCSeconds() !== second
  ) {
    throw new Error(errorMessage)
  }
  return `${yearText}-${monthText}-${dayText}T${hourText}:${minuteText}:${secondText}`
}

function optionalNonNegativeNumber(value: number | string | null | undefined, label: string): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed < 0) throw new Error(`${label}必须是大于或等于0的数字。`)
  return parsed
}

function buildReturnBuffer(
  hoursValue: number | string | null | undefined,
  minutesValue: number | string | null | undefined,
): number | null {
  const hoursEmpty = hoursValue === null || hoursValue === undefined || hoursValue === ''
  const minutesEmpty = minutesValue === null || minutesValue === undefined || minutesValue === ''
  if (hoursEmpty && minutesEmpty) return null

  const hours = hoursEmpty ? 0 : Number(hoursValue)
  const minutes = minutesEmpty ? 0 : Number(minutesValue)
  if (!Number.isSafeInteger(hours) || hours < 0) throw new Error('返程预留小时数必须是大于或等于0的整数。')
  if (!Number.isSafeInteger(minutes) || minutes < 0 || minutes > 59) throw new Error('返程预留分钟数必须是0到59的整数。')
  return hours * 3600 + minutes * 60
}

function optionalText(value: string): string | null {
  const trimmed = value.trim()
  return trimmed || null
}
