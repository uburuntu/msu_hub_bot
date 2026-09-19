export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status = 0,
  ) {
    super(message);
  }

  get uncertain(): boolean {
    return this.status === 0 || this.status >= 500;
  }
}
