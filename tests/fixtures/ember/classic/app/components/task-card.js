import Component from '@ember/component';
import { inject as service } from '@ember/service';

export default Component.extend({
  auditLog: service(),

  actions: {
    save() {
      this.get('auditLog').record('saved');
    }
  }
});
