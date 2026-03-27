#!/usr/bin/env python3
"""
Circuit Breaker Pattern for JembatanAI Proxy
Prevents cascading failures when providers are down
"""

import time
import logging
from enum import Enum
from typing import Callable, Any, Optional

log = logging.getLogger("circuit_breaker")

class CircuitState(Enum):
    CLOSED = "CLOSED"      # Normal operation
    OPEN = "OPEN"          # Failing, reject requests
    HALF_OPEN = "HALF_OPEN"  # Testing if recovered

class CircuitBreaker:
    """
    Circuit Breaker implementation.
    
    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Circuit tripped, requests fail immediately
    - HALF_OPEN: Testing recovery, one request allowed
    
    Transitions:
    - CLOSED → OPEN: When failure_threshold reached
    - OPEN → HALF_OPEN: After recovery_timeout
    - HALF_OPEN → CLOSED: On success
    - HALF_OPEN → OPEN: On failure
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        half_open_max_calls: int = 1
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._last_failure_time: float = 0
        self._half_open_calls = 0
        self._successes = 0
        self._total_calls = 0
    
    @property
    def state(self) -> CircuitState:
        """Get current state, auto-transition if needed."""
        if self._state == CircuitState.OPEN:
            # Check if recovery timeout passed
            if time.time() - self._last_failure_time > self.recovery_timeout:
                log.info(f"[{self.name}] Circuit breaker OPEN → HALF_OPEN")
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
        return self._state
    
    @property
    def is_closed(self) -> bool:
        return self.state == CircuitState.CLOSED
    
    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN
    
    @property
    def is_half_open(self) -> bool:
        return self.state == CircuitState.HALF_OPEN
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.
        
        Raises:
            CircuitBreakerOpen: If circuit is OPEN
        """
        self._total_calls += 1
        
        if self.state == CircuitState.OPEN:
            log.warning(f"[{self.name}] Circuit breaker OPEN - rejecting request")
            raise CircuitBreakerOpen(f"Circuit breaker {self.name} is OPEN")
        
        if self.state == CircuitState.HALF_OPEN:
            if self._half_open_calls >= self.half_open_max_calls:
                log.warning(f"[{self.name}] Circuit breaker HALF_OPEN - max calls reached")
                raise CircuitBreakerOpen(f"Circuit breaker {self.name} HALF_OPEN limit reached")
            self._half_open_calls += 1
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        
        except Exception as e:
            self._on_failure()
            raise
    
    async def call_async(self, func: Callable, *args, **kwargs) -> Any:
        """Execute async function with circuit breaker protection."""
        self._total_calls += 1
        
        if self.state == CircuitState.OPEN:
            log.warning(f"[{self.name}] Circuit breaker OPEN - rejecting request")
            raise CircuitBreakerOpen(f"Circuit breaker {self.name} is OPEN")
        
        if self.state == CircuitState.HALF_OPEN:
            if self._half_open_calls >= self.half_open_max_calls:
                log.warning(f"[{self.name}] Circuit breaker HALF_OPEN - max calls reached")
                raise CircuitBreakerOpen(f"Circuit breaker {self.name} HALF_OPEN limit reached")
            self._half_open_calls += 1
        
        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        
        except Exception as e:
            self._on_failure()
            raise
    
    def _on_success(self):
        """Handle successful call."""
        self._successes += 1
        
        if self.state == CircuitState.HALF_OPEN:
            log.info(f"[{self.name}] Circuit breaker HALF_OPEN → CLOSED (success)")
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._half_open_calls = 0
        
        elif self.state == CircuitState.CLOSED:
            # Reset failure count on success
            if self._failures > 0:
                self._failures = max(0, self._failures - 1)
    
    def _on_failure(self):
        """Handle failed call."""
        self._failures += 1
        self._last_failure_time = time.time()
        
        if self.state == CircuitState.HALF_OPEN:
            log.warning(f"[{self.name}] Circuit breaker HALF_OPEN → OPEN (failure)")
            self._state = CircuitState.OPEN
            self._half_open_calls = 0
        
        elif self.state == CircuitState.CLOSED:
            if self._failures >= self.failure_threshold:
                log.warning(f"[{self.name}] Circuit breaker CLOSED → OPEN ({self._failures} failures)")
                self._state = CircuitState.OPEN
    
    def get_stats(self) -> dict:
        """Get circuit breaker statistics."""
        return {
            "state": self.state.value,
            "failures": self._failures,
            "successes": self._successes,
            "total_calls": self._total_calls,
            "last_failure_time": self._last_failure_time,
        }
    
    def reset(self):
        """Reset circuit breaker to initial state."""
        log.info(f"[{self.name}] Circuit breaker reset")
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._last_failure_time = 0
        self._half_open_calls = 0

class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is OPEN."""
    pass

# Global circuit breakers for providers
_circuit_breakers = {}

def get_circuit_breaker(name: str) -> CircuitBreaker:
    """Get or create circuit breaker for provider."""
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(
            name=name,
            failure_threshold=5,
            recovery_timeout=60,
            half_open_max_calls=1
        )
    return _circuit_breakers[name]

def get_all_circuit_breakers() -> dict:
    """Get all circuit breaker stats."""
    return {name: cb.get_stats() for name, cb in _circuit_breakers.items()}

def reset_all_circuit_breakers():
    """Reset all circuit breakers."""
    for cb in _circuit_breakers.values():
        cb.reset()
