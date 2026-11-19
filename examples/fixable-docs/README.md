# Fixable documentation

Missing Python stdlib import:
```python
print(json.dumps({"hello": "world"}))
```

Python 2 print:
```python
print "hello from Python"
```

Removed collections alias (indented fence):
  ```python
  import collections
  print(isinstance({}, collections.Mapping))
  ```

Missing Node builtin:
```javascript
console.log(path.join('hello', 'world'));
```

Mixed module modes:
```javascript
import os from 'node:os';
const path = require('node:path');
console.log(path.sep, os.platform());
```

No deterministic fix:
```python
raise ValueError("application-specific failure")
```

Already passing:
```javascript
console.log('all good');
```
