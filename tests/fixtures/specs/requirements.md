# Payment Processing Requirements

## Overview

The payment processing module handles all financial transactions for the platform.

## Rules

1. All payment amounts must be positive integers in the smallest currency unit (cents).
2. The `process_payment` function must return one of: 200, 400, or 500.
   - 200: transaction approved
   - 400: invalid input (negative amount, missing fields)
   - 500: upstream payment provider error
3. Amounts above $10,000 (1,000,000 cents) require additional fraud checks.
4. Expired tokens must result in a 401 response.

## Error Handling

- `ValueError` must be raised when `amount` is negative or zero.
- `AuthError` must be raised when the token is expired or invalid.
- Network timeouts from the payment provider are wrapped in `PaymentProviderError`.
