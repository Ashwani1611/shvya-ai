'use strict';

// Redis ownership is per gateway, so two HTTP requests in this same process
// can both acquire it. Serialize browser/profile mutations per session too.
function createSessionOperationQueue() {
  const operations = new Map();
  return function withSessionOperation(sessionId, operation) {
    const previous = operations.get(sessionId) || Promise.resolve();
    const current = previous.catch(() => {}).then(operation);
    operations.set(sessionId, current);
    const cleanup = () => {
      if (operations.get(sessionId) === current) operations.delete(sessionId);
    };
    current.then(cleanup, cleanup);
    return current;
  };
}

module.exports = { createSessionOperationQueue };
