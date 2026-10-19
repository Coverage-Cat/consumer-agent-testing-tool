"use strict";
// Stands in for postman-collection, which Prism loads but CATT never uses. The
// upstream package pins Faker 5 and uuid 8, both with open advisories.
class Collection {
  constructor() {
    throw new Error("Postman collections are unsupported; supply an OpenAPI document");
  }
}

module.exports = { Collection };
