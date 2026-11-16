#pragma once

#include <cstdlib>
#include <optional>
#include <string>
#include <utility>

/// Sets an environment variable for one scope and puts back what was there,
/// unset included, however the scope ends.
/// @note setenv() is not thread-safe: hold one only while no other thread may
/// read the environment.
class ScopedEnvironmentVariable {
public:
  ScopedEnvironmentVariable(std::string name, const std::string &value)
      : name_(std::move(name)) {
    if (const char *previous = std::getenv(name_.c_str()))
      previous_ = previous;
    setenv(name_.c_str(), value.c_str(), 1);
  }

  ~ScopedEnvironmentVariable() {
    if (previous_)
      setenv(name_.c_str(), previous_->c_str(), 1);
    else
      unsetenv(name_.c_str());
  }

  ScopedEnvironmentVariable(const ScopedEnvironmentVariable &) = delete;
  ScopedEnvironmentVariable &
  operator=(const ScopedEnvironmentVariable &) = delete;

private:
  std::string name_;
  std::optional<std::string> previous_;
};
