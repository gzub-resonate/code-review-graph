import Mixin from '@ember/object/mixin';
import { inject as service } from '@ember/service';

export default Mixin.create({
  auditLog: service(),

  audit(message) {
    this.get('auditLog').record(message);
  }
});
