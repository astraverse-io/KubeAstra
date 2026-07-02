"""Input validation for Kubernetes resource names and parameters."""

import re
from typing import List, Optional

from config.settings import settings


class ValidationError(Exception):
    """Raised when input validation fails."""
    pass


# Kubernetes resource name regex (RFC 1123 DNS label)
# Must consist of lower case alphanumeric characters or '-',
# and must start and end with an alphanumeric character
K8S_NAME_PATTERN = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')

# Kubernetes node names may be DNS subdomains, so FQDN-style names with dots
# are valid even though most resource names are single DNS labels.
DNS_SUBDOMAIN_PATTERN = re.compile(
    r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$'
)

# More permissive pattern for label selectors
LABEL_SELECTOR_PATTERN = re.compile(r'^[a-zA-Z0-9]([-a-zA-Z0-9_.=/]*[a-zA-Z0-9])?$')


def validate_namespace(namespace: str) -> str:
    """
    Validate namespace name and check against allowlist.
    
    Args:
        namespace: Namespace to validate
        
    Returns:
        Validated namespace string
        
    Raises:
        ValidationError: If namespace is invalid or not allowed
    """
    if not namespace:
        raise ValidationError(
            "Namespace cannot be empty. "
            "Please specify a namespace from the allowed list."
        )
    
    if not isinstance(namespace, str):
        raise ValidationError(
            f"Namespace must be a string, got {type(namespace).__name__}"
        )
    
    if len(namespace) > 253:
        raise ValidationError(
            f"Namespace name too long: {len(namespace)} characters (max 253)"
        )
    
    if not K8S_NAME_PATTERN.match(namespace):
        raise ValidationError(
            f"Invalid namespace name: '{namespace}'. "
            "Kubernetes namespace names must consist of lowercase alphanumeric "
            "characters or '-', and must start and end with an alphanumeric character. "
            "Examples: 'prod', 'staging-env', 'dev2'"
        )
    
    allowed = settings.allowed_namespaces_list
    if "*" not in allowed and namespace not in allowed:
        raise ValidationError(
            f"Namespace '{namespace}' is not in the allowed list. "
            f"Allowed namespaces: {', '.join(allowed)}. "
            f"To allow all namespaces, set ALLOWED_NAMESPACES=* in your .env file."
        )
    
    return namespace


def validate_resource_name(name: str, resource_type: str = "resource") -> str:
    """
    Validate Kubernetes resource name.
    
    Args:
        name: Resource name to validate
        resource_type: Type of resource (for error messages)
        
    Returns:
        Validated name string
        
    Raises:
        ValidationError: If name is invalid
    """
    if not name:
        raise ValidationError(
            f"{resource_type.capitalize()} name cannot be empty. "
            f"Please provide a valid {resource_type} name."
        )
    
    if not isinstance(name, str):
        raise ValidationError(
            f"{resource_type.capitalize()} name must be a string, "
            f"got {type(name).__name__}"
        )
    
    if len(name) > 253:
        raise ValidationError(
            f"{resource_type.capitalize()} name too long: {len(name)} characters (max 253)"
        )
    
    if not K8S_NAME_PATTERN.match(name):
        raise ValidationError(
            f"Invalid {resource_type} name: '{name}'. "
            f"Kubernetes {resource_type} names must consist of lowercase alphanumeric "
            "characters or '-', and must start and end with an alphanumeric character. "
            f"Examples: 'my-{resource_type}', '{resource_type}-123', 'app-v2'"
        )
    
    return name


def validate_node_name(name: str) -> str:
    """Validate a Kubernetes node name, allowing DNS subdomain/FQDN names."""
    if not name:
        raise ValidationError(
            "Node name cannot be empty. Please provide a valid node name."
        )

    if not isinstance(name, str):
        raise ValidationError(
            f"Node name must be a string, got {type(name).__name__}"
        )

    if len(name) > 253:
        raise ValidationError(
            f"Node name too long: {len(name)} characters (max 253)"
        )

    if not DNS_SUBDOMAIN_PATTERN.match(name):
        raise ValidationError(
            f"Invalid node name: '{name}'. Kubernetes node names must be valid "
            "DNS subdomains: lowercase alphanumeric characters, '-', or '.', "
            "and each segment must start and end with an alphanumeric character."
        )

    return name


def validate_label_selector(selector: str) -> str:
    """
    Validate label selector format.
    
    Args:
        selector: Label selector string (e.g., "app=myapp,env=prod")
        
    Returns:
        Validated selector string
        
    Raises:
        ValidationError: If selector is invalid
    """
    if not selector:
        raise ValidationError("Label selector cannot be empty")
    
    if len(selector) > 1000:
        raise ValidationError("Label selector too long (max 1000 characters)")
    
    # Basic validation for common label selector patterns
    # Supports: key=value, key!=value, key in (value1,value2), etc.
    parts = selector.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        # Check for dangerous characters
        if any(char in part for char in [";", "&", "|", "`", "$", "()", "{}", "\n", "\r"]):
            raise ValidationError(f"Invalid characters in label selector: {part}")
    
    return selector


def validate_tail_lines(tail: int) -> int:
    """
    Validate and cap tail lines parameter.
    
    Args:
        tail: Number of lines to tail
        
    Returns:
        Validated and capped tail value
        
    Raises:
        ValidationError: If tail is invalid
    """
    if tail < 0:
        raise ValidationError("Tail lines cannot be negative")
    
    max_tail = settings.max_log_tail_lines
    if tail > max_tail:
        # Cap instead of error for better UX
        return max_tail
    
    return tail


def validate_environment_hint(environment: Optional[str]) -> Optional[str]:
    """
    Validate environment hint (prod, staging, dev, etc.).
    
    Args:
        environment: Environment hint string
        
    Returns:
        Validated environment string or None
        
    Raises:
        ValidationError: If environment contains invalid characters
    """
    if not environment:
        return None
    
    # Simple alphanumeric validation
    if not re.match(r'^[a-zA-Z0-9-_]+$', environment):
        raise ValidationError(
            f"Invalid environment hint: {environment}. "
            "Must contain only alphanumeric characters, hyphens, and underscores"
        )
    
    return environment.lower()


def get_allowed_namespaces() -> List[str]:
    """
    Get list of allowed namespaces.
    
    Returns:
        List of allowed namespace strings
    """
    return settings.allowed_namespaces_list
