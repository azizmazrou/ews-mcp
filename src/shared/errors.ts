export class AppError extends Error {
  public isOperational: boolean;
  public statusCode: number;
  public details?: any;

  constructor(message: string, statusCode = 500, details?: any) {
    super(message);
    this.statusCode = statusCode;
    this.isOperational = true;
    this.details = details;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class EWSRequestError extends AppError {}
export class EWSAuthenticationError extends AppError {}
export class EWSServerBusyError extends AppError {}
export class EWSNotFoundError extends AppError {}

export class InvalidToolInputError extends AppError {
  constructor(details: any) {
    super('Invalid tool input', 400, details);
  }
}
