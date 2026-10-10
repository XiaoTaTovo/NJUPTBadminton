"""Preordered per-target queues and an explicit serial round-robin cursor."""
class SerialQueues:
    def __init__(self, groups, key):
        self.groups=groups; self.key=key; self.cursor=0; self.queues={}; self.indices={}
        self.name_order={g['id']:{n:i for i,n in enumerate(g['courts'])} for g in groups}
    def rebuild(self, rows):
        for g in self.groups:
            names=self.name_order[g['id']]
            found={self.key(s):s for s in rows if s['available'] and s['date']==g['date']
                   and s['start']==g['start'] and s['end']==g['end'] and s['name'] in names}
            self.queues[g['id']]=sorted(found.values(),key=lambda s:names[s['name']])
            self.indices[g['id']]=0
    def take(self, completed, attempted, exhausted):
        for offset in range(len(self.groups)):
            index=(self.cursor+offset)%len(self.groups);g=self.groups[index]
            if g['id'] in exhausted or completed.get(g['id'],0)>=g['quantity']:continue
            q=self.queues[g['id']];i=self.indices[g['id']]
            while i<len(q) and self.key(q[i]) in attempted:i+=1
            self.indices[g['id']]=i
            if i<len(q):
                self.indices[g['id']]+=1;self.cursor=(index+1)%len(self.groups)
                return g,q[i]
        return None
