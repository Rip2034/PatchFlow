// Buggy Go — nil pointer dereference
package main

import "fmt"

type Config struct {
    Timeout *int
}

func getTimeout(cfg *Config) int {
    return *cfg.Timeout  // BUG: Timeout is nil → panic
}

func main() {
    cfg := &Config{}  // Timeout not initialized (= nil)
    result := getTimeout(cfg)
    fmt.Printf("Result: %d\n", result)
}
