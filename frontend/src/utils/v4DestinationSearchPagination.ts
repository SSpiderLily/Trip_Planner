interface DestinationSearchPageLike {
  page: number
  status: 'ok' | 'empty' | 'error' | string
  provider_count: number | null
  candidates: readonly unknown[]
  omitted_count: number
}

const DESTINATION_SEARCH_PAGE_SIZE = 20

/** The provider count describes raw results; an unknown count must not be inferred from screened candidates alone. */
export function canQueryNextDestinationPage(response: DestinationSearchPageLike): boolean {
  if (response.status === 'error') return false
  if (response.provider_count !== null) {
    return response.page * DESTINATION_SEARCH_PAGE_SIZE < response.provider_count
  }
  return response.candidates.length > 0 || response.omitted_count > 0
}

/** Keep a back path visible after empty/error responses from a page above the first one. */
export function shouldShowDestinationSearchPagination(response: DestinationSearchPageLike): boolean {
  return response.page > 1 || canQueryNextDestinationPage(response)
}
