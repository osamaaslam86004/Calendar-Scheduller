# Industry Analysis Insights You Can Query
With this logging table, you can execute analytics queries directly against PostgreSQL:  
## Cache Hit Ratio: 
Calculate `SUM(cached_tokens) / SUM(prompt_tokens)` per provider to monitor caching efficiency.
## Cost Savings Percentage: 
Compare actual cost vs. baseline cost without cache reads.
## Latency vs. Cache Hits: 
Evaluate `execution_time_ms` for cache hits vs. cache misses to prove latency improvements.